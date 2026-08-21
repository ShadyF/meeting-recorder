"""The single worker that connects recording completion to Speakr publication."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import queue
import re
import threading
import time
from typing import Any, Callable, NamedTuple, Protocol, cast
import unicodedata
import math

from .config import (
    Config,
    PublicationMode,
    load_config,
    require_speakr_token,
    resolve_speakr_url,
)
from .domain import CompletedRecording
from .network_manager import (
    NetworkManagerCancellation,
    NetworkManagerSSIDAdapter,
    NetworkSSIDResult,
    NetworkSSIDStatus,
)
from .speakr_domain import (
    MediaIdentity,
    PublicationJob,
    PublicationResult,
    PublicationState,
)
from .speakr_http import StdlibSpeakrTransport
from .speakr_publisher import SpeakrPublisher
from .speakr_store import PublicationStore, default_database_path


_DEFAULT_QUEUE_CAPACITY = 64
_DEFAULT_BATCH_SIZE = 10
_MAX_PERIODIC_SECONDS = 60.0
_NOTICE_MEMORY = 1024
_SAFE_ACTION = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_CONFIGURATION_ACTION = "configuration"
_PUBLICATION_ACTION = "publication"
_GENERIC_ERROR_CODE = "protocol_error"
_SAFE_NOTICE_ERROR_CODES = frozenset({
    "interrupted_transfer", "lease_expired", "local_missing",
    "metadata_ambiguous", "metadata_changed", "metadata_failed", "metadata_malformed",
    "metadata_missing", "metadata_unavailable", "protocol_error", "reconciliation_failed",
    "transfer_not_sent", "transfer_rejected", "transfer_unknown",
})

_NoticeVersion = int | tuple[int, int, int]


class _PublicationConfig(Protocol):
    @property
    def speakr_publication_mode(self) -> object:
        ...

    @property
    def speakr_allowed_ssid_bytes(self) -> tuple[bytes, ...]:
        ...


class _PublicationStore(Protocol):
    def update_path(
        self,
        old_private_path: str | bytes | os.PathLike[str],
        new_private_path: str | bytes | os.PathLike[str],
        identity: MediaIdentity,
    ) -> int:
        ...


class _PublicationPublisher(Protocol):
    def enqueue(self, path: Path | str, instance_url: str) -> PublicationJob:
        ...

    def due_job_ids(
        self, instance_url: str, *, now_ms: int | None = None, limit: int = 100,
    ) -> tuple[str, ...]:
        ...

    def next_wake_at_ms(
        self, instance_url: str, *, now_ms: int | None = None,
    ) -> int | None:
        ...

    def run_due(
        self, instance_url: str, token: str, limit: int = 100,
    ) -> list[PublicationResult]:
        ...

    def block_configuration(
        self, reference: str, *, instance_url: str | None = None,
    ) -> PublicationJob | None:
        ...


class _NetworkProbe(Protocol):
    def probe(self, cancellation: NetworkManagerCancellation) -> NetworkSSIDResult:
        ...


@dataclass(frozen=True)
class PublicationNotice:
    """A small safe action-required result for the caller's notification layer."""

    job_id: str | None
    state: PublicationState | None
    action: str
    error_code: str | None = None

    def __post_init__(self) -> None:
        # Keep the callback payload free of paths, credentials, metadata, and causes.
        if self.job_id is not None and (
            not isinstance(self.job_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", self.job_id) is None
        ):
            raise ValueError("notice job ID is invalid")
        if self.state is not None and not isinstance(self.state, PublicationState):
            raise ValueError("notice state is invalid")
        if not isinstance(self.action, str) or _SAFE_ACTION.fullmatch(self.action) is None:
            raise ValueError("notice action is invalid")
        if self.error_code is not None and (
            not isinstance(self.error_code, str)
            or self.error_code not in _SAFE_NOTICE_ERROR_CODES
        ):
            raise ValueError("notice error code is invalid")


class _Completion(NamedTuple):
    recording: CompletedRecording


class _Rename(NamedTuple):
    old_path: str | bytes
    new_path: str | bytes
    identity: MediaIdentity


class PublicationService:
    """Run one bounded, restart-safe publication worker.

    ``store_factory``, ``publisher_factory``, and ``network_factory`` are the
    only production seams.  Their default implementations are invoked from the
    worker, which keeps SQLite, HTTP, D-Bus, and file work away from GLib.
    """

    def __init__(
        self,
        config: _PublicationConfig | None = None,
        *,
        config_provider: Callable[[], object] = load_config,
        store_factory: Callable[[], _PublicationStore] | None = None,
        publisher_factory: Callable[[_PublicationStore, str], _PublicationPublisher] | None = None,
        network_factory: Callable[[tuple[bytes, ...]], _NetworkProbe] | None = None,
        token_provider: Callable[[], str] = require_speakr_token,
        notice_callback: Callable[[PublicationNotice], None] | None = None,
        queue_capacity: int = _DEFAULT_QUEUE_CAPACITY,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        periodic_check_seconds: float = _MAX_PERIODIC_SECONDS,
        clock_ms: Callable[[], int] | None = None,
        database_path_provider: Callable[[], Path] = default_database_path,
    ) -> None:
        # Validate only cheap immutable service configuration on the caller thread.
        if not callable(config_provider) or not callable(token_provider):
            raise ValueError("publication providers are invalid")
        if notice_callback is not None and not callable(notice_callback):
            raise ValueError("publication notice callback is invalid")
        if type(queue_capacity) is not int or queue_capacity < 1:
            raise ValueError("publication queue capacity is invalid")
        if type(batch_size) is not int or not 1 <= batch_size <= 100:
            raise ValueError("publication batch size is invalid")
        if (
            isinstance(periodic_check_seconds, bool)
            or not isinstance(periodic_check_seconds, (int, float))
            or not math.isfinite(float(periodic_check_seconds))
            or periodic_check_seconds <= 0
        ):
            raise ValueError("publication periodic check is invalid")
        if not callable(database_path_provider):
            raise ValueError("publication database path provider is invalid")

        # Read policy once per service lifetime so config changes apply on daemon restart.
        self._config_provider = config_provider
        self._config = config if config is not None else self._safe_config_read()
        self._mode = self._mode_for(self._config)
        self._store_factory = store_factory or (lambda: PublicationStore())
        self._publisher_factory = publisher_factory or (
            lambda store, _origin: SpeakrPublisher(
                cast(PublicationStore, store), StdlibSpeakrTransport(),
            )
        )
        self._network_factory = network_factory or (
            lambda allowed: NetworkManagerSSIDAdapter(allowed)
        )
        self._token_provider = token_provider
        self._notice_callback = notice_callback
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._database_path_provider = database_path_provider
        self._queue: queue.Queue[_Completion | _Rename] = queue.Queue(maxsize=queue_capacity)
        self._batch_size = batch_size
        self._periodic_seconds = min(float(periodic_check_seconds), _MAX_PERIODIC_SECONDS)

        self._state_lock = threading.Lock()
        self._network_gate = threading.Lock()
        self._wake = threading.Event()
        self._done = threading.Event()
        self._started = False
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._active_cancellation: NetworkManagerCancellation | None = None
        self._store: _PublicationStore | None = None
        self._publisher: _PublicationPublisher | None = None
        self._network: _NetworkProbe | None = None
        self._notice_keys: dict[
            tuple[str | None, str, str | None, _NoticeVersion], None
        ] = {}

    def start(self) -> None:
        """Start the daemon worker once; repeated calls are harmless."""
        # Serialize startup state changes so stop and repeated start cannot race.
        with self._state_lock:
            # Ignore repeated starts after the first state transition.
            if self._started:
                return

            # Mark startup complete before checking whether shutdown already won.
            self._started = True

            # Leave the service stopped when a caller quiesced it before startup.
            if self._stopping:
                return

            # Publish the thread reference before starting so a concurrent stop can join it.
            self._thread = threading.Thread(
                target=self._run, name="speakr-publication", daemon=True,
            )
            self._thread.start()

    def submit_completed(self, recording: CompletedRecording) -> bool:
        """Queue one completed recording only under the automatic policy."""
        if not isinstance(recording, CompletedRecording) or self._mode is not PublicationMode.AUTOMATIC:
            return False
        return self._submit(_Completion(recording))

    def submit_rename(
        self,
        old_absolute_path: str | bytes | os.PathLike[str],
        new_absolute_path: str | bytes | os.PathLike[str],
        identity: MediaIdentity,
    ) -> bool:
        """Queue an app-proven rename without inspecting either filesystem path."""
        old_path = self._absolute_path_argument(old_absolute_path)
        new_path = self._absolute_path_argument(new_absolute_path)
        if old_path is None or new_path is None or not isinstance(identity, MediaIdentity):
            return False
        return self._submit(_Rename(old_path, new_path, identity))

    def stop(self, timeout_seconds: float) -> bool:
        """Quiesce network work, drain bookkeeping, and bounded-join the worker."""
        # Reject invalid bounds before changing shared shutdown state.
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds < 0
        ):
            raise ValueError("publication stop timeout is invalid")

        # Capture the active work and ensure accepted bookkeeping has a worker.
        start_thread = False
        with self._state_lock:
            # Set quiescing before cancellation so no later worker check can start network work.
            self._stopping = True
            cancellation = self._active_cancellation
            thread = self._thread
            if thread is None and not self._started:
                # Start a short-lived worker so accepted bookkeeping is drained even before start().
                self._started = True
                thread = threading.Thread(
                    target=self._run, name="speakr-publication", daemon=True,
                )
                self._thread = thread
                start_thread = True
            self._wake.set()

        # Cancel an in-flight D-Bus operation after the service is marked as stopping.
        if cancellation is not None:
            cancellation.cancel()

        # A service with no worker has already reached its stopped state.
        if thread is None:
            return True

        # Start the drain worker only after publishing all shared state.
        if start_thread:
            thread.start()

        # Never deadlock when shutdown is requested from the worker itself.
        if thread is threading.current_thread():
            return False

        # Bound the final join and report whether shutdown completed in time.
        thread.join(float(timeout_seconds))
        return not thread.is_alive()

    def _safe_config_read(self) -> Config | None:
        # Treat a malformed optional publication configuration as disabled service state.
        try:
            value = self._config_provider()
        except Exception:
            return None
        return value if isinstance(value, Config) else None

    @staticmethod
    def _mode_for(config: _PublicationConfig | None) -> PublicationMode:
        # Unknown or unavailable configuration fails closed without touching the network.
        if config is None:
            return PublicationMode.DISABLED
        try:
            return PublicationMode.parse(config.speakr_publication_mode)
        except Exception:
            return PublicationMode.DISABLED

    @staticmethod
    def _absolute_path_argument(value: object) -> str | bytes | None:
        # Check only path shape; the worker and store perform all filesystem work later.
        if not isinstance(value, (str, bytes, os.PathLike)):
            return None
        try:
            rendered = os.fspath(value)
        except (TypeError, ValueError):
            return None
        if isinstance(rendered, str):
            return rendered if os.path.isabs(rendered) and "\x00" not in rendered else None
        if isinstance(rendered, bytes):
            return rendered if rendered.startswith(b"/") and b"\x00" not in rendered else None
        return None

    def _submit(self, item: _Completion | _Rename) -> bool:
        # Pair the queue check with the wake clear protocol so submissions cannot lose a wake.
        with self._state_lock:
            if self._stopping:
                return False
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                return False
            self._wake.set()
            return True

    def _run(self) -> None:
        # Keep every worker failure inside this thread and leave leases for restart recovery.
        try:
            while True:
                try:
                    # Drain callbacks before checking whether publication work may start.
                    self._drain_bookkeeping()
                    if self._is_stopping():
                        break
                    if self._mode is PublicationMode.DISABLED:
                        self._wait_for_wake(self._periodic_seconds)
                        continue
                    self._run_publication_cycle()
                except Exception:
                    # Isolate one broken cycle, then wait for a bounded retry or a new callback.
                    if self._is_stopping():
                        break
                    self._wait_for_wake(self._periodic_seconds)
        except Exception:
            # Keep even a failure in the recovery path from escaping this daemon thread.
            pass
        finally:
            with self._state_lock:
                self._active_cancellation = None
            self._done.set()

    def _drain_bookkeeping(self) -> None:
        # Process all currently queued work before considering a network deadline.
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                if isinstance(item, _Completion):
                    self._process_completion(item.recording)
                else:
                    self._process_rename(item)
            except Exception:
                # One bad recording or rename must not strand later bookkeeping.
                pass
            finally:
                self._queue.task_done()

    def _process_completion(self, recording: CompletedRecording) -> None:
        # Resolve the origin before hashing so invalid automatic config cannot create a job.
        config = self._config
        if config is None:
            self._emit_configuration_notice()
            return
        origin = self._resolve_origin()
        if origin is None:
            return
        publisher = self._ensure_publisher(origin)
        if publisher is None:
            self._emit_publication_failure_notice()
            return
        try:
            publisher.enqueue(recording.path, origin)
        except Exception:
            self._emit_publication_failure_notice()
            return

    def _process_rename(self, rename: _Rename) -> None:
        # Do not create an empty publication database for an untracked rename.
        try:
            database_path = self._database_path_provider()
            if not database_path.exists():
                return
        except Exception:
            return
        store = self._ensure_store()
        if store is None:
            return
        try:
            store.update_path(rename.old_path, rename.new_path, rename.identity)
        except Exception:
            return

    def _ensure_store(self) -> _PublicationStore | None:
        # Construct durable state only from the worker that owns all store calls.
        if self._store is not None:
            return self._store
        try:
            self._store = self._store_factory()
        except Exception:
            return None
        return self._store

    def _ensure_publisher(self, origin: str) -> _PublicationPublisher | None:
        # Construct the publisher and its production HTTP transport only in this worker.
        if self._publisher is not None:
            return self._publisher
        store = self._ensure_store()
        if store is None:
            return None
        try:
            self._publisher = self._publisher_factory(store, origin)
        except Exception:
            return None
        return self._publisher

    def _resolve_origin(self) -> str | None:
        # Convert every origin/configuration failure into one safe, deduplicated notice.
        config = self._config
        if config is None:
            self._emit_configuration_notice()
            return None
        try:
            return resolve_speakr_url(cast(Config, config))
        except Exception:
            self._emit_configuration_notice()
            return None

    def _emit_configuration_notice(self) -> None:
        # Configuration failures contain no job, path, network identity, or exception text.
        self._emit_notice(
            None, None, _CONFIGURATION_ACTION, _GENERIC_ERROR_CODE, 0,
        )

    def _emit_publication_failure_notice(self) -> None:
        # Optional publication failures remain visible without exposing local causes.
        self._emit_notice(
            None, None, _PUBLICATION_ACTION, _GENERIC_ERROR_CODE, 0,
        )

    def _run_publication_cycle(self) -> None:
        # A configured origin is the only durable-work scope admitted by this service.
        config = self._config
        if config is None:
            self._emit_configuration_notice()
            self._wait_for_wake(self._periodic_seconds)
            return
        origin = self._resolve_origin()
        if origin is None:
            self._wait_for_wake(self._periodic_seconds)
            return
        if not tuple(getattr(config, "speakr_allowed_ssid_bytes", ())):
            self._emit_configuration_notice()
            self._wait_for_wake(self._periodic_seconds)
            return
        publisher = self._ensure_publisher(origin)
        if publisher is None:
            self._emit_publication_failure_notice()
            self._wait_for_wake(self._periodic_seconds)
            return

        # Select a bounded due snapshot before any SSID or token operation.
        try:
            now = self._clock_ms()
            due_ids = tuple(publisher.due_job_ids(origin, now_ms=now, limit=self._batch_size))
        except Exception:
            self._wait_for_wake(self._periodic_seconds)
            return
        if due_ids:
            self._run_due_after_network(publisher, origin, due_ids)
            return

        # Use the earlier durable deadline or periodic cross-process discovery check.
        try:
            wake_at = publisher.next_wake_at_ms(origin, now_ms=self._clock_ms())
            delay = self._periodic_seconds
            if isinstance(wake_at, int):
                delay = min(delay, max(0.0, (wake_at - self._clock_ms()) / 1000.0))
        except Exception:
            delay = self._periodic_seconds
        self._wait_for_wake(delay)

    def _run_due_after_network(
        self, publisher: _PublicationPublisher, origin: str, due_ids: tuple[str, ...],
    ) -> None:
        # Probe admission only when a bounded due snapshot proves work exists.
        network_result = self._network_call(lambda cancellation: self._probe(cancellation))
        network_status = getattr(network_result, "status", network_result)
        try:
            network_status = NetworkSSIDStatus(network_status)
        except (TypeError, ValueError):
            network_status = NetworkSSIDStatus.UNAVAILABLE
        if network_status is not NetworkSSIDStatus.ALLOWED:
            self._wait_for_wake(self._periodic_seconds)
            return

        # Fetch credentials after admission and never retain them between cycles.
        if self._is_stopping():
            return
        token: str | None
        try:
            candidate = self._token_provider()
            token = candidate if self._valid_token(candidate) else None
        except Exception:
            token = None
        if token is None:
            for job_id in due_ids:
                if self._is_stopping() and self._queue.empty():
                    break
                self._block_configuration(publisher, job_id, origin)
            return

        # Run one bounded publisher batch; individual result notices are isolated.
        if self._is_stopping():
            return
        try:
            results = self._network_call(
                lambda _cancellation: publisher.run_due(
                    origin, token, limit=self._batch_size,
                )
            )
        except Exception:
            results = None
        if results is not None:
            for result in results:
                self._emit_result(result)

    def _block_configuration(
        self, publisher: _PublicationPublisher, job_id: str, origin: str,
    ) -> None:
        # Use the publisher's shared fenced operation so no token or staging can be persisted.
        try:
            blocked = publisher.block_configuration(job_id, instance_url=origin)
        except Exception:
            return
        self._emit_job(blocked)

    @staticmethod
    def _valid_token(value: object) -> bool:
        # Match the process-token contract without retaining or exposing the credential.
        return (
            isinstance(value, str)
            and bool(value)
            and len(value) <= 4096
            and not any(
                char.isspace() or unicodedata.category(char).startswith("C")
                for char in value
            )
        )

    def _probe(self, cancellation: NetworkManagerCancellation) -> Any:
        # Instantiate NetworkManager lazily so disabled mode never touches D-Bus setup.
        config = self._config
        if config is None:
            return NetworkSSIDResult(NetworkSSIDStatus.UNAVAILABLE)
        if self._network is None:
            allowed = tuple(config.speakr_allowed_ssid_bytes)
            self._network = self._network_factory(allowed)
        network = self._network
        if network is None:
            return NetworkSSIDResult(NetworkSSIDStatus.UNAVAILABLE)
        return network.probe(cancellation)

    def _network_call(self, operation: Callable[[NetworkManagerCancellation], Any]) -> Any:
        # Reserve the network gate before checking quiescing, closing the stop race.
        with self._network_gate:
            with self._state_lock:
                if self._stopping:
                    return None
                cancellation = NetworkManagerCancellation()
                self._active_cancellation = cancellation
            try:
                return operation(cancellation)
            finally:
                with self._state_lock:
                    if self._active_cancellation is cancellation:
                        self._active_cancellation = None

    def _emit_result(self, result: Any) -> None:
        # Convert only publisher results into the bounded public notice shape.
        if isinstance(result, PublicationResult):
            self._emit_job(result.job)
        else:
            self._emit_job(getattr(result, "job", None))

    def _emit_job(self, job: Any) -> None:
        # Notify only terminal action-required states, never active reconciliation.
        if not isinstance(job, PublicationJob):
            return
        if job.state is PublicationState.BLOCKED:
            action = "blocked"
        elif job.state is PublicationState.MISSING:
            action = "missing"
        elif job.state is PublicationState.UNCERTAIN and not job.reconciliation_eligible:
            action = "uncertain"
        else:
            return
        progress = (job.updated_at_ms, job.attempt_count, job.lease_generation)
        self._emit_notice(job.job_id, job.state, action, job.last_error_code, progress)

    def _emit_notice(
        self,
        job_id: str | None,
        state: PublicationState | None,
        action: str,
        error_code: str | None,
        version: _NoticeVersion,
    ) -> None:
        # Deduplicate transitions with bounded memory before invoking user code.
        key = (job_id, action, error_code, version)
        if key in self._notice_keys:
            return
        self._notice_keys[key] = None
        if len(self._notice_keys) > _NOTICE_MEMORY:
            self._notice_keys.pop(next(iter(self._notice_keys)))
        callback = self._notice_callback
        if callback is None:
            return
        try:
            callback(PublicationNotice(job_id, state, action, error_code))
        except Exception:
            pass

    def _is_stopping(self) -> bool:
        with self._state_lock:
            return self._stopping

    def _wait_for_wake(self, seconds: float) -> None:
        # Clear the old signal under the submit/stop lock before releasing it to wait.
        with self._state_lock:
            if self._stopping or not self._queue.empty():
                return
            self._wake.clear()
        # Submit and stop set the event while holding the same lock, so a new signal cannot be cleared.
        self._wake.wait(max(0.0, seconds))


__all__ = ["PublicationNotice", "PublicationService"]
