"""Focused CLI tests for the explicit Speakr decision-record forms."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from meeting_recorder.__main__ import (
    _cmd_speakr_upload, _publication_rename_tracker, build_parser, main,
)
from meeting_recorder.speakr_domain import MediaIdentity, PublicationKey, PublicationState
from meeting_recorder.speakr_store import PublicationStore
from meeting_recorder.network_manager import NetworkSSIDStatus


ORIGIN = "https://configured.example"
TOKEN = "cli-token-private-sentinel"
HASH = "a" * 64
ALLOWED_SSIDS = (b"test-network",)


def _job(
    job_id: str = "job-1",
    state: PublicationState = PublicationState.QUEUED,
    *,
    operation: str = "post",
    resume_intent: str = "post",
    origin: str = ORIGIN,
):
    # Return the smallest job-shaped object needed by the command handlers.
    return SimpleNamespace(
        job_id=job_id,
        state=state,
        operation=operation,
        resume_intent=resume_intent,
        key=SimpleNamespace(instance_url=origin, recording_sha256=HASH),
        attempt_count=2,
        next_attempt_at_ms=1234,
        remote_recording_id=42,
        last_error_code="transfer_unknown",
        last_http_status=503,
        reconciliation_eligible=operation == "reconcile",
    )


def _result(job, already_published: bool = False):
    return SimpleNamespace(job=job, already_published=already_published)


class FakePublisher:
    def __init__(self, jobs=()) -> None:
        # Keep calls and returned jobs in memory so tests can inspect CLI routing.
        self.jobs = {job.job_id: job for job in jobs}
        self.calls: list[tuple] = []
        self.next_result = _result(_job("job-1", PublicationState.PUBLISHED))

    def get(self, reference):
        # Record local lookup requests without contacting a transport.
        self.calls.append(("get", reference))
        return self.jobs.get(reference)

    def list(self):
        # Record local list requests and return the current fake rows.
        self.calls.append(("list",))
        return list(self.jobs.values())

    def enqueue(self, path, origin):
        # Create a fake queued row for path-form command tests.
        self.calls.append(("enqueue", path, origin))
        job = _job()
        self.jobs[job.job_id] = job
        return job

    def run_one(self, origin, token, reference):
        # Record one-job execution with the credentials supplied by the CLI.
        self.calls.append(("run_one", origin, token, reference))
        return self.next_result

    def run_due(self, origin, token):
        # Record bounded execution requests from the fake engine.
        self.calls.append(("run_due", origin, token))
        return [self.next_result]

    def run_all_due(self, origin, token):
        # Record fixed-snapshot batch execution requests.
        self.calls.append(("run_all_due", origin, token))
        return [self.next_result]

    def retry(self, reference):
        # Record explicit local retry resets.
        self.calls.append(("retry", reference))
        return self.jobs.get(reference, _job(reference))

    def block_configuration(self, reference, *, instance_url=None):
        # Model engine-side fencing when credentials are unavailable.
        self.calls.append(("block_configuration", reference))
        job = self.jobs.get(reference, _job(reference, PublicationState.BLOCKED))
        job.state = PublicationState.BLOCKED
        job.last_error_code = "protocol_error"
        return job

    def relink(self, reference, path):
        # Record local path replacement without running publication.
        self.calls.append(("relink", reference, path))
        return self.jobs.get(reference, _job(reference))

    def forget(self, reference):
        # Remove only the local fake row after recording the action.
        self.calls.append(("forget", reference))
        self.jobs.pop(reference, None)


def _output(callable_object, *args, **kwargs) -> tuple[int, str]:
    # Capture both streams so command errors are checked without terminal output.
    output = io.StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        result = callable_object(*args, **kwargs)
    return result, output.getvalue()


def _run_with_fake(fake: FakePublisher, **kwargs) -> tuple[int, str]:
    # Replace configuration and construction so each test observes only CLI calls.
    # These legacy routing tests use force; focused runtime tests cover NetworkManager admission.
    if kwargs.get("path") is not None or kwargs.get("all_jobs") or kwargs.get("retry_job"):
        kwargs.setdefault("force", True)
    with patch("meeting_recorder.__main__._speakr_publisher", return_value=fake), \
            patch("meeting_recorder.__main__.resolve_speakr_url", return_value=ORIGIN), \
            patch("meeting_recorder.__main__.require_speakr_token", return_value=TOKEN):
        return _output(
            _cmd_speakr_upload,
            SimpleNamespace(speakr_url=ORIGIN, speakr_allowed_ssid_bytes=ALLOWED_SSIDS),
            **kwargs,
        )


def test_speakr_parser_exposes_all_decision_forms_and_rejects_credential_flags() -> None:
    # Build the parser once, then exercise every supported decision form.
    parser = build_parser()
    forms = (
        ["recording.mkv"], ["--all"], ["--status", "JOB"],
        ["--status", "--all"], ["--retry", "JOB"],
        ["--relink", "JOB", "new.mkv"], ["--forget", "JOB"],
    )

    # Every accepted form must select the Speakr upload command.
    for form in forms:
        args = parser.parse_args(["speakr", "upload", *form])
        assert args.command == "speakr" and args.speakr_command == "upload"

    # Credential flags remain rejected because credentials come from configuration.
    for option in ("--token", "--url", "--secret"):
        try:
            parser.parse_args(["speakr", "upload", option, "value"])
        except SystemExit:
            pass
        else:
            raise AssertionError(f"forbidden Speakr option accepted: {option}")


def test_status_and_status_all_are_local_and_secret_free() -> None:
    # Status for one job must not resolve credentials or contact the network.
    job = _job()
    fake = FakePublisher((job,))
    with patch("meeting_recorder.__main__._speakr_publisher", return_value=fake), \
            patch("meeting_recorder.__main__.require_speakr_token", side_effect=AssertionError):
        result, output = _output(
            _cmd_speakr_upload, SimpleNamespace(speakr_url=ORIGIN), status_job=job.job_id,
        )
        assert result == 0
        assert job.job_id in output and HASH in output
        assert TOKEN not in output and "Design review" not in output
        assert not any(call[0] == "run_one" for call in fake.calls)

    # Listing all jobs follows the same local, secret-free path.
    fake = FakePublisher((job,))
    with patch("meeting_recorder.__main__._speakr_publisher", return_value=fake), \
            patch("meeting_recorder.__main__.require_speakr_token", side_effect=AssertionError):
        result, output = _output(
            _cmd_speakr_upload, SimpleNamespace(speakr_url=ORIGIN), all_jobs=True, status_all=True,
        )
    assert result == 0 and job.job_id in output and TOKEN not in output


def test_path_upload_and_all_use_configured_origin_and_token() -> None:
    # A path upload must enqueue and run with the configured origin and token.
    fake = FakePublisher()
    result, output = _run_with_fake(fake, path="recording.mkv")
    assert result == 0 and "published" in output
    assert ("enqueue", "recording.mkv", ORIGIN) in fake.calls
    assert any(call[:3] == ("run_one", ORIGIN, TOKEN) for call in fake.calls)

    # The all-jobs form must use the batch operation with the same credentials.
    fake = FakePublisher()
    result, output = _run_with_fake(fake, all_jobs=True)
    assert result == 0 and "published" in output
    assert any(call[:3] == ("run_all_due", ORIGIN, TOKEN) for call in fake.calls)


def test_retry_warns_for_terminal_uncertain_and_runs_only_after_explicit_reset() -> None:
    # An uncertain job requires an explicit retry reset before execution.
    job = _job("uncertain", PublicationState.UNCERTAIN, operation="none", resume_intent="reconcile")
    fake = FakePublisher((job,))
    result, output = _run_with_fake(fake, retry_job=job.job_id)
    assert result == 0
    assert "duplicate" in output
    assert fake.calls[:3] == [("get", job.job_id), ("retry", job.job_id),
                               ("run_one", ORIGIN, TOKEN, job.job_id)]
    assert TOKEN not in output


def test_missing_token_uses_engine_fencing_to_block_network_jobs() -> None:
    # A missing token still reaches the engine, which blocks the path job safely.
    blocked = _job("blocked", PublicationState.BLOCKED)
    fake = FakePublisher((blocked,))
    fake.next_result = _result(blocked)
    with patch("meeting_recorder.__main__._speakr_publisher", return_value=fake), \
            patch("meeting_recorder.__main__.resolve_speakr_url", return_value=ORIGIN), \
            patch("meeting_recorder.__main__.require_speakr_token", side_effect=ValueError):
        result, output = _output(
            _cmd_speakr_upload, SimpleNamespace(speakr_url=ORIGIN), path="recording.mkv",
            force=True,
        )
    assert result == 1 and "blocked" in output
    assert ("block_configuration", "job-1") in fake.calls
    assert TOKEN not in output

    # The all-jobs form applies the same empty-token fencing to its batch call.
    fake = FakePublisher((blocked,))
    fake.next_result = _result(blocked)
    with patch("meeting_recorder.__main__._speakr_publisher", return_value=fake), \
            patch("meeting_recorder.__main__.resolve_speakr_url", return_value=ORIGIN), \
            patch("meeting_recorder.__main__.require_speakr_token", side_effect=ValueError):
        result, output = _output(
            _cmd_speakr_upload, SimpleNamespace(speakr_url=ORIGIN), all_jobs=True,
            force=True,
        )
    assert result == 1 and ("run_all_due", ORIGIN, "") in fake.calls

    # Retry without credentials blocks the referenced job instead of resetting it.
    fake = FakePublisher((blocked,))
    with patch("meeting_recorder.__main__._speakr_publisher", return_value=fake), \
            patch("meeting_recorder.__main__.resolve_speakr_url", return_value=ORIGIN), \
            patch("meeting_recorder.__main__.require_speakr_token", side_effect=ValueError):
        result, output = _output(
            _cmd_speakr_upload, SimpleNamespace(speakr_url=ORIGIN), retry_job=blocked.job_id,
            force=True,
        )
    assert result == 1 and ("block_configuration", blocked.job_id) in fake.calls
    assert not any(call[0] == "retry" for call in fake.calls)


def test_missing_origin_does_not_guess_or_create_a_new_path_job() -> None:
    # A missing configured origin must fail before enqueueing local state.
    fake = FakePublisher()
    with patch("meeting_recorder.__main__._speakr_publisher", return_value=fake), \
            patch("meeting_recorder.__main__.resolve_speakr_url", side_effect=ValueError), \
            patch("meeting_recorder.__main__.require_speakr_token", side_effect=AssertionError):
        result, output = _output(
            _cmd_speakr_upload, SimpleNamespace(speakr_url=None), path="recording.mkv",
        )
    assert result == 2
    assert not any(call[0] == "enqueue" for call in fake.calls)
    assert TOKEN not in output


def test_relink_and_forget_are_local_and_report_invalid_references() -> None:
    # Relinking changes only local state and must not run a publication.
    job = _job()
    fake = FakePublisher((job,))
    result, output = _run_with_fake(fake, relink_job=job.job_id, relink_path="new.mkv")
    assert result == 0 and "new.mkv" not in output
    assert ("relink", job.job_id, "new.mkv") in fake.calls
    assert not any(call[0] == "run_one" for call in fake.calls)

    # Forgetting also stays local and reports the requested action.
    result, output = _run_with_fake(fake, forget_job=job.job_id)
    assert result == 0 and "forgot" in output
    assert ("forget", job.job_id) in fake.calls

    # A missing local reference is reported as a command error.
    missing = FakePublisher()
    result, output = _run_with_fake(missing, status_job="missing")
    assert result == 2 and "not found" in output


def test_action_required_results_and_ambiguous_forms_are_nonzero() -> None:
    # Blocked execution returns a nonzero result and exposes the next action.
    fake = FakePublisher()
    fake.next_result = _result(_job(state=PublicationState.BLOCKED))
    result, output = _run_with_fake(fake, path="recording.mkv")
    assert result == 1 and "action required" in output and "blocked" in output

    # Supplying both path and batch forms is rejected as ambiguous.
    fake = FakePublisher()
    result, output = _output(
        _cmd_speakr_upload, SimpleNamespace(speakr_url=ORIGIN), path="recording.mkv", all_jobs=True,
    )
    assert result == 2 and "PATH" in output


def test_parser_rejects_status_without_job_or_all_and_status_job_with_all() -> None:
    # Invalid status combinations must terminate through parser validation.
    for argv in (
        ["speakr", "upload", "--status"],
        ["speakr", "upload", "--status", "JOB", "--all"],
    ):
        try:
            main(argv)
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError("ambiguous Speakr form was accepted")


def test_force_is_limited_to_network_operations() -> None:
    # The parser accepts force alongside each candidate operation form.
    parser = build_parser()
    for arguments in (
        ["recording.mkv", "--force"], ["--all", "--force"], ["--retry", "JOB", "--force"],
    ):
        assert parser.parse_args(["speakr", "upload", *arguments]).force

    # Local inspection and mutation forms reject force before publisher construction.
    for kwargs in (
        {"status_job": "JOB"}, {"status_all": True, "all_jobs": True},
        {"relink_job": "JOB", "relink_path": "new.mkv"}, {"forget_job": "JOB"},
    ):
        result, output = _output(
            _cmd_speakr_upload, SimpleNamespace(speakr_url=ORIGIN), force=True, **kwargs,
        )
        assert result == 2 and "--force" in output


def test_normal_network_admission_fails_closed_without_reading_token() -> None:
    # Each denied status leaves a path job queued and stops before token lookup.
    statuses = (NetworkSSIDStatus.DISALLOWED, "unexpected", NetworkSSIDStatus.UNAVAILABLE)
    for status in statuses:
        fake = FakePublisher()
        adapter_calls = []

        class Adapter:
            def __init__(self, allowed):
                adapter_calls.append(allowed)

            def probe(self):
                return SimpleNamespace(status=status)

        with patch("meeting_recorder.__main__._speakr_publisher", return_value=fake), \
                patch("meeting_recorder.__main__.resolve_speakr_url", return_value=ORIGIN), \
                patch("meeting_recorder.__main__.require_speakr_token",
                      side_effect=AssertionError("token must not be read")), \
                patch("meeting_recorder.network_manager.NetworkManagerSSIDAdapter", Adapter):
            result, output = _output(
                _cmd_speakr_upload,
                SimpleNamespace(speakr_url=ORIGIN, speakr_allowed_ssid_bytes=(b"allowed",)),
                path="recording.mkv",
            )

        assert result == 3 and "waiting" in output
        assert any(call[0] == "enqueue" for call in fake.calls)
        assert adapter_calls == [(b"allowed",)]

    # Missing and empty allowlists fail closed before any adapter or token work.
    for allowlist in (None, ()):
        fake = FakePublisher()
        with patch("meeting_recorder.__main__._speakr_publisher", return_value=fake), \
                patch("meeting_recorder.__main__.resolve_speakr_url", return_value=ORIGIN), \
                patch("meeting_recorder.__main__.require_speakr_token",
                      side_effect=AssertionError("token must not be read")), \
                patch("meeting_recorder.network_manager.NetworkManagerSSIDAdapter",
                      side_effect=AssertionError("adapter must not be constructed")):
            result, _output_text = _output(
                _cmd_speakr_upload,
                SimpleNamespace(speakr_url=ORIGIN, speakr_allowed_ssid_bytes=allowlist),
                path="recording.mkv",
            )
        assert result == 3 and any(call[0] == "enqueue" for call in fake.calls)


def test_normal_retry_admission_does_not_reset_the_job() -> None:
    # A retry is inspected locally but remains untouched while the network is denied.
    job = _job("retry")
    fake = FakePublisher((job,))

    class Adapter:
        def __init__(self, _allowed):
            pass

        def probe(self):
            return SimpleNamespace(status=NetworkSSIDStatus.DISALLOWED)

    with patch("meeting_recorder.__main__._speakr_publisher", return_value=fake), \
            patch("meeting_recorder.__main__.resolve_speakr_url", return_value=ORIGIN), \
            patch("meeting_recorder.__main__.require_speakr_token",
                  side_effect=AssertionError("token must not be read")), \
            patch("meeting_recorder.network_manager.NetworkManagerSSIDAdapter", Adapter):
        result, _output_text = _output(
            _cmd_speakr_upload,
            SimpleNamespace(speakr_url=ORIGIN, speakr_allowed_ssid_bytes=(b"allowed",)),
            retry_job=job.job_id,
        )

    assert result == 3
    assert not any(call[0] in ("retry", "run_one") for call in fake.calls)


def test_no_due_all_skips_network_adapter_and_token() -> None:
    # A local empty due snapshot avoids D-Bus and credential work entirely.
    class DuePublisher(FakePublisher):
        def due_job_ids(self, _origin, *, limit):
            self.calls.append(("due_job_ids", limit))
            return ()

    fake = DuePublisher()
    with patch("meeting_recorder.__main__._speakr_publisher", return_value=fake), \
            patch("meeting_recorder.__main__.resolve_speakr_url", return_value=ORIGIN), \
            patch("meeting_recorder.__main__.require_speakr_token",
                  side_effect=AssertionError("token must not be read")), \
            patch("meeting_recorder.network_manager.NetworkManagerSSIDAdapter",
                  side_effect=AssertionError("adapter must not be constructed")):
        result, output = _output(
            _cmd_speakr_upload,
            SimpleNamespace(speakr_url=ORIGIN, speakr_allowed_ssid_bytes=(b"allowed",)),
            all_jobs=True,
        )

    assert result == 0 and "no due" in output
    assert ("due_job_ids", 100) in fake.calls
    assert not any(call[0] == "run_all_due" for call in fake.calls)


def test_force_skips_only_network_admission() -> None:
    # Force bypasses the SSID adapter but keeps origin, token, and engine checks.
    fake = FakePublisher()
    resolved, tokens = [], []

    def resolve(_cfg):
        resolved.append(True)
        return ORIGIN

    def token():
        tokens.append(True)
        return TOKEN

    with patch("meeting_recorder.__main__._speakr_publisher", return_value=fake), \
            patch("meeting_recorder.__main__.resolve_speakr_url", side_effect=resolve), \
            patch("meeting_recorder.__main__.require_speakr_token", side_effect=token), \
            patch("meeting_recorder.network_manager.NetworkManagerSSIDAdapter",
                  side_effect=AssertionError("force must skip adapter")):
        result, _output_text = _output(
            _cmd_speakr_upload,
            SimpleNamespace(speakr_url=ORIGIN, speakr_allowed_ssid_bytes=()),
            path="recording.mkv", force=True,
        )

    assert result == 0 and resolved == [True] and tokens == [True]
    assert any(call[:3] == ("run_one", ORIGIN, TOKEN) for call in fake.calls)


def test_cli_rename_tracker_updates_matching_unleased_identity_without_creating_new_state() -> None:
    # Use an isolated state directory so rename tracking cannot affect other jobs.
    with TemporaryDirectory() as directory:
        root = Path(directory)
        state_home = root / "state-home"
        old = root / "old.mkv"
        new = root / "new.mkv"
        old.write_bytes(b"recording")
        info = os.lstat(old)
        identity = MediaIdentity(old, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        database = state_home / "meeting-recorder" / "publications.sqlite3"
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(state_home)}, clear=False):
            # Create one matching identity, rename it, and update the existing row.
            store = PublicationStore(database)
            key = PublicationKey(ORIGIN, hashlib.sha256(b"recording").hexdigest())
            job = store.create_or_reuse(key, os.fsencode(old), identity=identity)
            old.rename(new)
            _publication_rename_tracker()(old, new)
            updated = PublicationStore(database).get(job.job_id)

        # The tracker must preserve the job while replacing only its local path.
        assert updated is not None and updated.private_path == os.fsencode(new)
