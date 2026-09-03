"""Focused tests for the isolated Speakr publication worker."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from threading import Event, Thread, current_thread
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import time

from meeting_recorder.config import PublicationMode
from meeting_recorder.domain import CaptureMode, CompletedRecording
from meeting_recorder.network_manager import (
    NetworkManagerCancellation,
    NetworkSSIDResult,
    NetworkSSIDStatus,
)
from meeting_recorder.speakr_domain import (
    MediaIdentity,
    PublicationJob,
    PublicationKey,
    PublicationOperation,
    PublicationResult,
    PublicationState,
    ResumeIntent,
)
from meeting_recorder.speakr_service import PublicationNotice, PublicationService


ORIGIN = "https://example.com"


def _config(
    mode: PublicationMode,
    url: str | None = ORIGIN,
    allowed: tuple[bytes, ...] = (b"allowed",),
):
    # Keep the worker tests independent from user configuration files.
    return SimpleNamespace(
        speakr_publication_mode=mode,
        speakr_url=url,
        speakr_allowed_ssid_bytes=allowed,
    )


def _completed(path: Path) -> CompletedRecording:
    # The worker receives the immutable finalized recording produced by the recorder.
    from datetime import datetime, timezone

    # Build a valid UTC completion without touching media contents.
    now = datetime.now(timezone.utc)
    return CompletedRecording(path, "test", CaptureMode.AUDIO_ONLY, True, False, now, now)


class FakeStore:
    def __init__(self, constructed: list[str]) -> None:
        # Record construction so tests can prove worker ownership of storage.
        self.constructed = constructed
        self.constructed.append("store")
        self.renames: list[tuple[object, object, MediaIdentity]] = []

    def update_path(self, old, new, identity) -> int:
        # Preserve each worker rename observation for the test assertions.
        self.renames.append((old, new, identity))
        return 1


class FakePublisher:
    def __init__(self, calls: list[tuple[str, str]]) -> None:
        # Keep publisher state in memory while exposing worker call order.
        self.calls = calls
        self.enqueued: list[Path] = []
        self.due = ["job-1"]
        self.blocked: list[str] = []
        self.enqueue_error: Exception | None = None

    def enqueue(self, path, origin):
        # Record enqueue ownership before applying the injected failure.
        self.calls.append(("enqueue", current_thread().name))
        if self.enqueue_error is not None:
            raise self.enqueue_error
        self.enqueued.append(Path(path))

    def due_job_ids(self, origin, *, now_ms, limit):
        # Return one bounded due item, then model an empty later snapshot.
        result, self.due = self.due[:limit], []
        return tuple(result)

    def next_wake_at_ms(self, origin, *, now_ms):
        return None

    def run_due(self, origin, token, *, limit):
        # Record batch execution without performing network work.
        self.calls.append(("run_due", current_thread().name))
        return []

    def block_configuration(self, job_id, *, instance_url):
        # Preserve the IDs fenced after token admission fails.
        self.blocked.append(job_id)
        return None


class FakeNetwork:
    def __init__(self, status: NetworkSSIDStatus, calls: list[str]) -> None:
        # Store only the safe status and an order list for admission assertions.
        self.status = status
        self.calls = calls

    def probe(self, cancellation: NetworkManagerCancellation):
        # Report the injected status without opening a bus connection.
        self.calls.append("probe")
        return NetworkSSIDResult(self.status)


class ControlledWake:
    """Make the clear/submit/wait interleaving deterministic."""

    def __init__(self) -> None:
        # Expose both the wake signal and the point where the worker begins waiting.
        self.event = Event()
        self.cleared = Event()

    def clear(self) -> None:
        # Make the wait window observable before clearing the signal.
        self.event.clear()
        self.cleared.set()

    def set(self) -> None:
        self.event.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self.event.wait(timeout)


def test_mode_matrix_accepts_only_automatic_completions_and_renames_everywhere() -> None:
    # Keep all temporary recording paths within one isolated test directory.
    with TemporaryDirectory() as directory:
        tmp_path = Path(directory)

        # Completion admission is policy-controlled while rename bookkeeping is always accepted.
        recording = _completed(tmp_path / "recording.mkv")
        identity = MediaIdentity(recording.path, 1, 2, 3, 4)
        for mode, accepted in (
            (PublicationMode.DISABLED, False),
            (PublicationMode.MANUAL, False),
            (PublicationMode.AUTOMATIC, True),
        ):
            service = PublicationService(_config(mode), queue_capacity=2)
            assert service.submit_completed(recording) is accepted
            assert service.submit_rename(recording.path, tmp_path / "renamed.mkv", identity)
            assert service.stop(2) is True


def test_non_config_provider_output_fails_closed() -> None:
    # A provider that returns a duck-typed value must not become runtime config.
    service = PublicationService(
        config_provider=lambda: SimpleNamespace(speakr_publication_mode=PublicationMode.AUTOMATIC),
    )

    assert service._config is None
    assert service._mode is PublicationMode.DISABLED


def test_disabled_wait_submit_interleaving_is_not_lost() -> None:
    # Keep the marker database and rename paths in one isolated test directory.
    with TemporaryDirectory() as directory:
        tmp_path = Path(directory)

        # A rename submitted after wait preparation wakes the disabled worker immediately.
        database = tmp_path / "publications.sqlite3"
        database.touch()
        constructed: list[str] = []
        old = tmp_path / "old.mkv"
        identity = MediaIdentity(old, 1, 2, 3, 4)
        service = PublicationService(
            _config(PublicationMode.DISABLED),
            store_factory=lambda: FakeStore(constructed),
            database_path_provider=lambda: database,
            periodic_check_seconds=60,
        )
        wake = ControlledWake()
        service._wake = wake  # type: ignore[assignment]
        service.start()
        assert wake.cleared.wait(2)
        assert service.submit_rename(old, tmp_path / "new.mkv", identity)
        deadline = time.time() + 2
        while constructed == [] and time.time() < deadline:
            time.sleep(0.01)
        assert constructed == ["store"]
        assert service.stop(2)


def test_disallowed_ssid_wait_submit_interleaving_is_not_lost() -> None:
    # Keep the marker database and rename paths in one isolated test directory.
    with TemporaryDirectory() as directory:
        tmp_path = Path(directory)

        # The same protocol wakes a disallowed-SSID worker for queued bookkeeping.
        database = tmp_path / "publications.sqlite3"
        database.touch()
        constructed: list[str] = []
        publisher = FakePublisher([])
        publisher.due = ["job-1"]
        service = PublicationService(
            _config(PublicationMode.AUTOMATIC),
            store_factory=lambda: FakeStore(constructed),
            publisher_factory=lambda store, origin: publisher,
            network_factory=lambda allowed: FakeNetwork(NetworkSSIDStatus.DISALLOWED, []),
            database_path_provider=lambda: database,
            periodic_check_seconds=60,
        )
        wake = ControlledWake()
        service._wake = wake  # type: ignore[assignment]
        service.start()
        assert wake.cleared.wait(2)  # next_wake is checked after the failed admission probe
        old = tmp_path / "old.mkv"
        identity = MediaIdentity(old, 1, 2, 3, 4)
        assert service.submit_rename(old, tmp_path / "new.mkv", identity)
        deadline = time.time() + 2
        while constructed == [] and time.time() < deadline:
            time.sleep(0.01)
        assert constructed == ["store"]
        assert service.stop(2)


def test_completion_enqueue_runs_on_worker_and_start_is_idempotent() -> None:
    # Keep the submitted recording path in an isolated temporary directory.
    with TemporaryDirectory() as directory:
        tmp_path = Path(directory)

        # The submitter performs only a bounded queue operation; publisher work runs elsewhere.
        calls: list[tuple[str, str]] = []
        publisher = FakePublisher(calls)

        def make_publisher(store, origin):
            return publisher

        service = PublicationService(
            _config(PublicationMode.AUTOMATIC),
            store_factory=lambda: FakeStore([]),
            publisher_factory=make_publisher,
        )
        service.start()
        service.start()
        submitter = current_thread().name
        assert service.submit_completed(_completed(tmp_path / "recording.mkv"))
        deadline = time.time() + 2
        while not calls and time.time() < deadline:
            time.sleep(0.01)
        assert calls and calls[0][0] == "enqueue" and calls[0][1] != submitter
        assert service.stop(2)


def test_startup_due_work_checks_ssid_before_token_and_runs_bounded_batch() -> None:
    # Due work is admitted in the worker and credentials are read only after an allowed probe.
    order: list[str] = []
    publisher = FakePublisher([])

    class OrderedPublisher(FakePublisher):
        def due_job_ids(self, origin, *, now_ms, limit):
            order.append("due")
            return super().due_job_ids(origin, now_ms=now_ms, limit=limit)

        def run_due(self, origin, token, *, limit):
            order.append("run")
            return super().run_due(origin, token, limit=limit)

    publisher = OrderedPublisher([])

    def provide_token() -> str:
        order.append("token")
        return "token"

    service = PublicationService(
        _config(PublicationMode.AUTOMATIC),
        store_factory=lambda: FakeStore([]),
        publisher_factory=lambda store, origin: publisher,
        network_factory=lambda allowed: FakeNetwork(NetworkSSIDStatus.ALLOWED, order),
        token_provider=provide_token,
        periodic_check_seconds=0.01,
    )
    service.start()
    deadline = time.time() + 2
    while "run" not in order and time.time() < deadline:
        time.sleep(0.01)
    assert order[:4] == ["due", "probe", "token", "run"]
    assert service.stop(2)


def test_invalid_origin_is_deduplicated_and_constructs_no_runtime() -> None:
    # Keep the submitted recording path in an isolated temporary directory.
    with TemporaryDirectory() as directory:
        tmp_path = Path(directory)

        # A bad origin is action-required before store, publisher, D-Bus, or token access.
        notices: list[PublicationNotice] = []
        constructed: list[str] = []
        token_calls: list[str] = []

        def fail_store():
            constructed.append("store")
            raise AssertionError("store must not be constructed")

        def fail_token() -> str:
            token_calls.append("token")
            raise AssertionError("token must not be fetched")

        service = PublicationService(
            _config(PublicationMode.AUTOMATIC, url="http://insecure.example"),
            store_factory=fail_store,
            publisher_factory=lambda store, origin: (_ for _ in ()).throw(
                AssertionError("publisher must not be constructed")
            ),
            network_factory=lambda allowed: (_ for _ in ()).throw(
                AssertionError("NetworkManager must not be constructed")
            ),
            token_provider=fail_token,
            notice_callback=notices.append,
            periodic_check_seconds=0.01,
        )
        assert service.submit_completed(_completed(tmp_path / "recording.mkv"))
        service.start()
        deadline = time.time() + 2
        while not notices and time.time() < deadline:
            time.sleep(0.01)
        assert len(notices) == 1
        assert (notices[0].action, notices[0].error_code) == ("configuration", "protocol_error")
        assert constructed == [] and token_calls == []
        assert service.stop(2)


def test_empty_allowlist_disables_the_gate_without_network_probe() -> None:
    # An intentionally empty list lets due work run without constructing NetworkManager.
    calls: list[str] = []
    publisher = FakePublisher([])
    service = PublicationService(
        _config(PublicationMode.AUTOMATIC, allowed=()),
        store_factory=lambda: FakeStore(calls),
        publisher_factory=lambda store, origin: publisher,
        network_factory=lambda allowed: (_ for _ in ()).throw(
            AssertionError("NetworkManager must not be constructed")
        ),
        token_provider=lambda: "token",
        periodic_check_seconds=0.01,
    )
    service.start()
    deadline = time.time() + 2
    while not publisher.calls and time.time() < deadline:
        time.sleep(0.01)
    assert publisher.calls == [("run_due", "speakr-publication")]
    assert service.stop(2)


def test_non_wifi_bypass_runs_due_work_after_token_admission() -> None:
    # A confirmed non-Wi-Fi result follows the same admitted path as an allowed SSID.
    publisher = FakePublisher([])
    service = PublicationService(
        _config(PublicationMode.AUTOMATIC),
        store_factory=lambda: FakeStore([]),
        publisher_factory=lambda store, origin: publisher,
        network_factory=lambda allowed: FakeNetwork(NetworkSSIDStatus.BYPASSED, []),
        token_provider=lambda: "token",
        periodic_check_seconds=0.01,
    )
    service.start()
    deadline = time.time() + 2
    while not publisher.calls and time.time() < deadline:
        time.sleep(0.01)
    assert publisher.calls == [("run_due", "speakr-publication")]
    assert service.stop(2)


def test_cycle_failure_does_not_kill_worker_and_later_rename_succeeds() -> None:
    # Keep the marker database and rename paths in one isolated test directory.
    with TemporaryDirectory() as directory:
        tmp_path = Path(directory)

        # An unexpected cycle error gets a bounded retry instead of terminating the worker.
        database = tmp_path / "publications.sqlite3"
        database.touch()
        constructed: list[str] = []
        publisher = FakePublisher([])
        publisher.due = []
        service = PublicationService(
            _config(PublicationMode.AUTOMATIC),
            store_factory=lambda: FakeStore(constructed),
            publisher_factory=lambda store, origin: publisher,
            network_factory=lambda allowed: FakeNetwork(NetworkSSIDStatus.ALLOWED, []),
            database_path_provider=lambda: database,
            periodic_check_seconds=0.05,
        )
        original_cycle = service._run_publication_cycle
        failed = [False]

        def fail_once() -> None:
            if not failed[0]:
                failed[0] = True
                raise RuntimeError("deliberate cycle failure")
            original_cycle()

        service._run_publication_cycle = fail_once  # type: ignore[method-assign]
        service.start()
        time.sleep(0.02)
        old = tmp_path / "old.mkv"
        identity = MediaIdentity(old, 1, 2, 3, 4)
        assert service.submit_rename(old, tmp_path / "new.mkv", identity)
        deadline = time.time() + 2
        while constructed == [] and time.time() < deadline:
            time.sleep(0.01)
        assert failed[0] and constructed == ["store"]
        assert service.stop(2)


def test_completion_runtime_failure_is_visible_once_and_later_work_runs() -> None:
    # Keep submitted recording paths in one isolated temporary directory.
    with TemporaryDirectory() as directory:
        tmp_path = Path(directory)

        # A transient enqueue dependency failure emits one safe notice and does not affect recording success.
        notices: list[PublicationNotice] = []
        publisher = FakePublisher([])
        publisher.due = []
        attempts = [0]

        def make_publisher(store, origin):
            attempts[0] += 1
            if attempts[0] == 1:
                raise RuntimeError("private construction failure")
            return publisher

        service = PublicationService(
            _config(PublicationMode.AUTOMATIC),
            store_factory=lambda: FakeStore([]),
            publisher_factory=make_publisher,
            notice_callback=notices.append,
            periodic_check_seconds=0.01,
        )
        service.start()
        assert service.submit_completed(_completed(tmp_path / "first.mkv"))
        deadline = time.time() + 2
        while not notices and time.time() < deadline:
            time.sleep(0.01)
        assert len(notices) == 1
        assert (notices[0].action, notices[0].error_code) == ("publication", "protocol_error")
        publisher.enqueue_error = RuntimeError("private enqueue failure")
        assert service.submit_completed(_completed(tmp_path / "second-fails.mkv"))
        time.sleep(0.05)
        assert len(notices) == 1
        publisher.enqueue_error = None
        assert service.submit_completed(_completed(tmp_path / "second.mkv"))
        deadline = time.time() + 2
        while len(publisher.enqueued) < 1 and time.time() < deadline:
            time.sleep(0.01)
        assert len(publisher.enqueued) == 1
        assert service.stop(2)


def test_non_admitted_network_does_not_fetch_token_or_run_publisher() -> None:
    # Every non-admitted SSID status leaves durable work untouched and waits for a later check.
    for status in (
        NetworkSSIDStatus.DISALLOWED,
        NetworkSSIDStatus.UNKNOWN,
        NetworkSSIDStatus.UNAVAILABLE,
    ):
        token_calls: list[str] = []

        def provide_token() -> str:
            token_calls.append("token")
            return "token"

        publisher = FakePublisher([])
        def make_network(allowed: tuple[bytes, ...], selected=status) -> FakeNetwork:
            return FakeNetwork(selected, [])

        service = PublicationService(
            _config(PublicationMode.AUTOMATIC),
            store_factory=lambda: FakeStore([]),
            publisher_factory=lambda store, origin: publisher,
            network_factory=make_network,
            token_provider=provide_token,
        )
        service.start()
        time.sleep(0.05)
        assert token_calls == []
        assert publisher.calls == []
        assert service.stop(2)


def test_missing_token_uses_bounded_configuration_blocking() -> None:
    # Missing credentials block only the selected due IDs through the publisher seam.
    publisher = FakePublisher([])

    def missing_token() -> str:
        raise ValueError("missing token")

    service = PublicationService(
        _config(PublicationMode.AUTOMATIC),
        store_factory=lambda: FakeStore([]),
        publisher_factory=lambda store, origin: publisher,
        network_factory=lambda allowed: FakeNetwork(NetworkSSIDStatus.ALLOWED, []),
        token_provider=missing_token,
    )
    service.start()
    deadline = time.time() + 2
    while not publisher.blocked and time.time() < deadline:
        time.sleep(0.01)
    assert publisher.blocked == ["job-1"]
    assert service.stop(2)


def test_invalid_token_blocks_without_network_body_and_notices_are_private() -> None:
    # Use a real shaped job result to verify only safe terminal fields cross the callback.
    notices: list[PublicationNotice] = []
    key = PublicationKey(ORIGIN, "a" * 64)
    job = PublicationJob(
        "job-1", key, state=PublicationState.BLOCKED,
        operation=PublicationOperation.NONE.value,
        resume_intent=ResumeIntent.POST.value,
        private_path=b"/private/meeting.mkv", last_error_code="protocol_error",
        created_at_ms=1, updated_at_ms=2, blocked_at_ms=2,
    )
    service = PublicationService(_config(PublicationMode.DISABLED), notice_callback=notices.append)
    service._emit_job(job)
    service._emit_job(job)
    assert len(notices) == 1
    assert notices[0].job_id == "job-1"
    assert "private" not in repr(notices[0])
    assert notices[0].state is PublicationState.BLOCKED
    try:
        PublicationNotice(None, None, "configuration", "raw_exception")
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe notice error code was accepted")


def test_job_notice_dedupe_uses_attempt_progress_when_timestamps_match() -> None:
    # Deduplicate repeated delivery while retaining a later durable retry transition.
    notices: list[PublicationNotice] = []
    key = PublicationKey(ORIGIN, "b" * 64)
    first = PublicationJob(
        "job-1", key, state=PublicationState.BLOCKED,
        operation=PublicationOperation.NONE.value,
        resume_intent=ResumeIntent.POST.value,
        last_error_code="protocol_error", created_at_ms=1, updated_at_ms=2,
        blocked_at_ms=2, attempt_count=1, lease_generation=1,
    )
    later = replace(first, attempt_count=2, lease_generation=2)
    service = PublicationService(_config(PublicationMode.DISABLED), notice_callback=notices.append)

    # The exact same durable transition is delivered twice, then a later retry is delivered.
    service._emit_job(first)
    service._emit_job(first)
    service._emit_job(later)

    assert len(notices) == 2


def test_rename_does_not_create_store_when_default_database_is_absent() -> None:
    # Keep rename paths in one isolated temporary directory.
    with TemporaryDirectory() as directory:
        tmp_path = Path(directory)

        # Disabled rename handling must preserve the no-empty-state rule.
        constructed: list[str] = []
        old = tmp_path / "old.mkv"
        new = tmp_path / "new.mkv"
        identity = MediaIdentity(old, 1, 2, 3, 4)
        service = PublicationService(
            _config(PublicationMode.DISABLED),
            store_factory=lambda: FakeStore(constructed),
            database_path_provider=lambda: tmp_path / "missing.sqlite3",
        )
        assert service.submit_rename(old, new, identity)
        service.start()
        time.sleep(0.05)
        assert constructed == []
        assert service.stop(2)


def test_queue_overflow_is_nonblocking_and_stop_drains_rename() -> None:
    # Keep the marker database and rename paths in one isolated test directory.
    with TemporaryDirectory() as directory:
        tmp_path = Path(directory)

        # A full queue rejects immediately, while an accepted rename is still processed before exit.
        constructed: list[str] = []
        old = tmp_path / "old.mkv"
        new = tmp_path / "new.mkv"
        identity = MediaIdentity(old, 1, 2, 3, 4)
        database = tmp_path / "publications.sqlite3"
        database.touch()
        service = PublicationService(
            _config(PublicationMode.DISABLED),
            store_factory=lambda: FakeStore(constructed),
            database_path_provider=lambda: database,
            queue_capacity=1,
        )
        assert service.submit_rename(old, new, identity)
        assert not service.submit_rename(old, new, identity)
        service.start()
        assert service.stop(2)
        assert constructed == ["store"]
