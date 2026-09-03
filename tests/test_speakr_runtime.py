"""Offline runtime integration tests for recording and daemon Speakr wiring."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import stat
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from meeting_recorder.config import PublicationMode
from meeting_recorder.domain import CaptureMode, CompletedRecording
from meeting_recorder.network_manager import NetworkSSIDStatus
from meeting_recorder.speakr_domain import MediaIdentity


ORIGIN = "https://configured.example"


def _completed() -> CompletedRecording:
    # Use one valid immutable completion for both one-shot and daemon paths.
    started = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
    return CompletedRecording(
        Path("capture.mkv"), "Manual", CaptureMode.AUDIO_ONLY, True, False,
        started, datetime(2026, 8, 21, 12, 1, tzinfo=timezone.utc),
    )


def _daemon_config() -> SimpleNamespace:
    # Keep daemon setup entirely in memory while satisfying its logging and detector inputs.
    return SimpleNamespace(
        allowlist=(), poll_interval_seconds=1, start_debounce_seconds=1,
        stop_debounce_seconds=1, google_calendar_client_id=None,
        google_calendar_loopback_port=True,
        speakr_publication_mode=PublicationMode.AUTOMATIC,
        speakr_allowed_ssid_bytes=(b"allowed",),
    )


def _run_daemon(service_class, controller_class, loop_action, events):
    # Replace every runtime boundary so this helper does not load GI, D-Bus, HTTP, or storage.
    class GLib:
        PRIORITY_HIGH = 1
        idle_callbacks = []

        class MainLoop:
            def run(self):
                # Drive the supplied scenario while the fake GLib loop is active.
                events.append("loop-run")
                loop_action()

            def quit(self):
                # Record loop shutdown requests without entering a real main loop.
                events.append("loop-quit")

        @staticmethod
        def timeout_add(*_args):
            return 1

        @staticmethod
        def unix_signal_add(*_args):
            return 1

        @classmethod
        def idle_add(cls, callback, *args):
            cls.idle_callbacks.append((callback, args))
            return len(cls.idle_callbacks)

    class CalendarOAuth:
        def __init__(self, _cfg):
            # Keep calendar setup independent from OAuth and network services.
            pass

        @property
        def access_token(self):
            # Supply a harmless calendar credential only to finish daemon setup.
            return "calendar-token"

    class CalendarService:
        def __init__(self, *_args):
            # Record optional calendar construction without starting a worker.
            events.append("calendar-construct")

        def start(self):
            # Record the normal optional-service startup phase.
            events.append("calendar-start")

        def stop(self, _timeout):
            # Let the daemon proceed through its normal cleanup ordering.
            events.append("calendar-stop")
            return True

    class Recorder:
        def __init__(self, _cfg):
            # Avoid constructing an actual capture process.
            events.append("recorder")

    class Detector:
        def __init__(self, **_kwargs):
            # Keep detector construction observable while using no audio APIs.
            events.append("detector")

        def tick(self):
            # Report a healthy poll without reading system streams.
            return True

    class Notifier:
        instance = None

        def __init__(self):
            # Keep notification calls in memory for idle-dispatch assertions.
            self.calls = []
            Notifier.instance = self

        def info(self, title, body, **kwargs):
            # Capture the public notification payload without GTK.
            self.calls.append((title, body, kwargs))

    gi = types.ModuleType("gi")
    setattr(gi, "require_version", lambda *_args: None)
    repository = types.ModuleType("gi.repository")
    setattr(repository, "GLib", GLib)

    with ExitStack() as stack:
        # Install fake GI modules before _cmd_run performs its local imports.
        stack.enter_context(patch.dict(sys.modules, {"gi": gi, "gi.repository": repository}))
        stack.enter_context(patch("meeting_recorder.speakr_service.PublicationService", service_class))
        stack.enter_context(patch("meeting_recorder.notifier.Notifier", Notifier))
        stack.enter_context(patch("meeting_recorder.recorder.Recorder", Recorder))
        stack.enter_context(patch("meeting_recorder.controller.Controller", controller_class))
        stack.enter_context(patch("meeting_recorder.detector.MeetingDetector", Detector))
        stack.enter_context(patch("meeting_recorder.calendar_oauth.CalendarOAuth", CalendarOAuth))
        stack.enter_context(patch("meeting_recorder.calendar_google.GoogleCalendarClient", lambda token: token))
        stack.enter_context(patch("meeting_recorder.calendar_refresh.CalendarRefresher", lambda *args: args))
        stack.enter_context(patch("meeting_recorder.calendar_cache.CalendarCache", lambda: object()))
        stack.enter_context(patch("meeting_recorder.calendar_service.CalendarRefreshService", CalendarService))
        from meeting_recorder.__main__ import _cmd_run
        result = _cmd_run(_daemon_config())

    return result, GLib, Notifier.instance


def test_daemon_completion_service_notice_and_shutdown_are_isolated() -> None:
    # Construct a service that reports two identical transitions and accepts one completion.
    events = []
    completed = _completed()

    class Service:
        instance = None

        def __init__(self, _cfg, *, notice_callback):
            # Save the callback so startup can emit deterministic notices.
            events.append("publication-construct")
            self.notice_callback = notice_callback
            Service.instance = self

        def start(self):
            # Emit two identical transitions to exercise independent idle dispatch.
            events.append("publication-start")
            notice = SimpleNamespace(
                action="publication", job_id="job-1", error_code="transfer_unknown",
            )
            self.notice_callback(notice)
            self.notice_callback(notice)

        def submit_completed(self, recording):
            # Record immutable completion admission without starting a worker.
            events.append(("submit", recording))
            return True

        def stop(self, _timeout):
            # Return a timeout result while preserving shutdown ordering.
            events.append("publication-stop")
            return False

    class Controller:
        instance = None

        def __init__(self, *_args, **_kwargs):
            # Keep controller construction visible to the startup-isolation test.
            events.append("controller-construct")
            self.on_finished = None
            Controller.instance = self

        def on_meeting_start(self, *_args):
            pass

        def on_meeting_stop(self, *_args):
            pass

        def shutdown(self):
            # Record controller cleanup before publication cleanup.
            events.append("controller-shutdown")

    def finish_recording():
        # The controller invokes the daemon callback exactly once after finalization.
        assert Controller.instance is not None
        assert Controller.instance.on_finished is not None
        Controller.instance.on_finished(completed)

    result, glib, notifier = _run_daemon(Service, Controller, finish_recording, events)

    # Completion admission is the only publication action caused by on_finished.
    assert result == 0
    assert [event[0] for event in events if isinstance(event, tuple)] == ["submit"]
    assert events.index("controller-shutdown") < events.index("publication-stop")

    # Each worker notice gets its own GLib callback, and notification runs only there.
    assert len(glib.idle_callbacks) == 2
    assert notifier is not None
    assert notifier.calls == []
    for callback, args in glib.idle_callbacks:
        assert callback(*args) is False
    assert len(notifier.calls) == 2
    assert all("/" not in body and "capture.mkv" not in body for _, body, _ in notifier.calls)


def test_daemon_publication_start_failure_does_not_block_recording() -> None:
    # A failed optional service is cleaned up while recorder startup continues.
    events = []

    class BrokenService:
        def __init__(self, *_args, **_kwargs):
            # Construct successfully so only service startup fails.
            events.append("publication-construct")

        def start(self):
            # Fail at the optional boundary that must not block recording.
            events.append("publication-start")
            raise RuntimeError("unavailable")

        def stop(self, _timeout):
            # Allow failed-start cleanup to complete.
            events.append("publication-stop")
            return True

    class Controller:
        instance = None

        def __init__(self, *_args, **_kwargs):
            events.append("controller-construct")
            Controller.instance = self
            self.on_finished = None

        def on_meeting_start(self, *_args):
            pass

        def on_meeting_stop(self, *_args):
            pass

        def shutdown(self):
            events.append("controller-shutdown")

    result, _glib, _notifier = _run_daemon(
        BrokenService, Controller, lambda: None, events,
    )

    assert result == 0
    assert events.index("recorder") < events.index("loop-run")
    assert events.count("controller-shutdown") == 1
    assert events.count("publication-stop") == 1


def test_daemon_tag_requester_resolves_operation_token_without_passing_config() -> None:
    # Capture the real daemon requester through a controller that performs no GTK work.
    events = []
    received = []

    class Service:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def stop(self, _timeout):
            return True

    class Controller:
        instance = None

        def __init__(self, *_args, tag_requester, **_kwargs):
            self.on_finished = None
            self.tag_requester = tag_requester
            Controller.instance = self

        def on_meeting_start(self, *_args):
            pass

        def on_meeting_stop(self, *_args):
            pass

        def shutdown(self):
            pass

    class TagService:
        instance = None

        def __init__(self, *_args):
            self.requests = []
            TagService.instance = self

        def activate(self, origin):
            self.origin = origin

        def request(self, origin, token, callback):
            self.requests.append((origin, token, callback))
            return object()

        def shutdown(self, _timeout):
            return True

    class TagCache:
        def activate(self, _origin):
            pass

    # Invoke one credentialed request and then one failed request from the captured seam.
    def request_tags() -> None:
        assert Controller.instance is not None
        assert Controller.instance.tag_requester is not None
        assert Controller.instance.tag_requester(received.append) is not None
        assert Controller.instance.tag_requester(received.append) is None

    token_calls = 0

    def resolve_token() -> str:
        nonlocal token_calls
        token_calls += 1
        if token_calls == 1:
            return "token-placeholder"
        raise ValueError("credential unavailable")

    with ExitStack() as stack:
        # Replace catalog boundaries while leaving _cmd_run's requester construction real.
        stack.enter_context(patch("meeting_recorder.__main__.resolve_speakr_url", return_value=ORIGIN))
        stack.enter_context(patch("meeting_recorder.__main__.require_speakr_token", side_effect=resolve_token))
        stack.enter_context(patch("meeting_recorder.speakr_http.StdlibSpeakrTransport", lambda: object()))
        stack.enter_context(patch("meeting_recorder.speakr_tag_cache.SpeakrTagCache", TagCache))
        stack.enter_context(patch("meeting_recorder.speakr_tag_service.SpeakrTagService", TagService))
        result, _glib, _notifier = _run_daemon(Service, Controller, request_tags, events)

    # The service receives the one operation-scoped token and original callback.
    assert result == 0 and TagService.instance is not None
    assert TagService.instance.origin == ORIGIN
    assert TagService.instance.requests == [(ORIGIN, "token-placeholder", received.append)]

    # Credential failure maps to the controller's existing safe unavailable outcome.
    assert len(received) == 1
    assert received[0].tags == () and received[0].unavailable_notice


def test_daemon_shutdown_isolated_when_publication_stop_raises() -> None:
    # A worker stop exception must not hide controller cleanup or change the exit code.
    events = []

    class Service:
        def __init__(self, *_args, **_kwargs):
            # Keep the failing-stop service constructor side-effect free.
            pass

        def start(self):
            # Start successfully so shutdown reaches the stop failure.
            pass

        def stop(self, _timeout):
            # Raise only after recording controller cleanup has been observed.
            events.append("publication-stop")
            raise RuntimeError("stop failed")

    class Controller:
        def __init__(self, *_args, **_kwargs):
            self.on_finished = None

        def on_meeting_start(self, *_args):
            pass

        def on_meeting_stop(self, *_args):
            pass

        def shutdown(self):
            events.append("controller-shutdown")

    result, _glib, _notifier = _run_daemon(Service, Controller, lambda: None, events)

    assert result == 0
    assert events.index("controller-shutdown") < events.index("publication-stop")


def test_daemon_rename_tracker_submits_lstat_identity_without_store_access() -> None:
    # The GLib-side callback only performs absolute-path conversion and one cheap lstat.
    from meeting_recorder.__main__ import _daemon_publication_rename_tracker

    calls = []

    class Service:
        def submit_rename(self, old_path, new_path, identity):
            # Capture the worker submission without opening publication storage.
            calls.append((old_path, new_path, identity))

    info = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o600, st_dev=7, st_ino=8, st_size=9, st_mtime_ns=10,
    )
    with patch("meeting_recorder.__main__.os.lstat", return_value=info), \
            patch("meeting_recorder.__main__.PublicationStore",
                  side_effect=AssertionError("store must stay on worker"), create=True):
        _daemon_publication_rename_tracker(Service())(Path("old.mkv"), Path("new.mkv"))

    assert len(calls) == 1
    old_path, new_path, identity = calls[0]
    assert old_path.is_absolute() and new_path.is_absolute()
    assert isinstance(identity, MediaIdentity)
    assert identity.path == new_path and (identity.device, identity.inode, identity.size,
                                          identity.mtime_ns) == (7, 8, 9, 10)


def _run_record(mode: PublicationMode, complete: bool):
    # Replace the one-shot command's GLib, recorder, notifier, and controller boundaries.
    events = []
    completed = _completed()

    class GLib:
        PRIORITY_HIGH = 1

        class MainLoop:
            def run(self):
                # Complete or cancel the fake manual recording from the loop thread.
                assert Controller.instance is not None
                if complete:
                    assert Controller.instance.on_finished is not None
                    Controller.instance.on_finished(completed)
                else:
                    assert Controller.instance.on_manual_cancelled is not None
                    Controller.instance.on_manual_cancelled()

            def quit(self):
                # Record the command's request to leave the fake loop.
                events.append("loop-quit")

        @staticmethod
        def timeout_add_seconds(*_args):
            return 1

        @staticmethod
        def unix_signal_add(*_args):
            return 1

    class Notifier:
        has_live_notifications = False

    class Recorder:
        def __init__(self, _cfg):
            # Avoid starting any real capture process.
            pass

    from meeting_recorder.controller import State

    class Controller:
        instance = None

        def __init__(self, *_args, **_kwargs):
            # Expose the callback slots used by the one-shot command.
            self.state = State.IDLE
            self.on_finished = None
            self.on_manual_cancelled = None
            Controller.instance = self

        def start_manual(self, _app):
            # Move directly to recording state without portal setup.
            self.state = State.RECORDING

        def stop_manual(self):
            # Keep the stop seam available even though scenarios finish in the loop.
            events.append("stop-manual")
            return True

        def shutdown(self):
            # Record cleanup before the post-loop publication admission.
            events.append("controller-shutdown")

    class Enricher:
        def __init__(self, *_args, **_kwargs):
            # Preserve completion objects without sidecar or filesystem work.
            self.enrich = lambda recording: recording

    class Publisher:
        def enqueue(self, path, origin):
            # Record the final path and origin after controller shutdown.
            events.append(("enqueue", path, origin))

    cfg = SimpleNamespace(speakr_publication_mode=mode, speakr_url=ORIGIN)
    gi = types.ModuleType("gi")
    setattr(gi, "require_version", lambda *_args: None)
    repository = types.ModuleType("gi.repository")
    setattr(repository, "GLib", GLib)

    with ExitStack() as stack:
        # Keep this one-shot test independent of token, transport, and publication storage code.
        stack.enter_context(patch.dict(sys.modules, {"gi": gi, "gi.repository": repository}))
        stack.enter_context(patch("meeting_recorder.notifier.Notifier", Notifier))
        stack.enter_context(patch("meeting_recorder.recorder.Recorder", Recorder))
        stack.enter_context(patch("meeting_recorder.controller.Controller", Controller))
        stack.enter_context(patch("meeting_recorder.recording_enrichment.RecordingEnricher", Enricher))
        stack.enter_context(patch("meeting_recorder.__main__._speakr_publisher", return_value=Publisher()))
        stack.enter_context(patch("meeting_recorder.__main__.resolve_speakr_url",
                                 side_effect=AssertionError("record must not resolve URL")
                                 if mode is not PublicationMode.AUTOMATIC else
                                 (lambda _cfg: ORIGIN)))
        stack.enter_context(patch("meeting_recorder.__main__.require_speakr_token",
                                 side_effect=AssertionError("record must not read token")))
        from meeting_recorder.__main__ import _cmd_record
        result = _cmd_record(cfg)

    return result, events


def test_record_completion_is_preserved_and_automatic_queueing_follows_shutdown() -> None:
    # Automatic publication queues exactly once after GLib and controller shutdown.
    result, events = _run_record(PublicationMode.AUTOMATIC, True)

    assert result == 0
    assert events[-1] == ("enqueue", Path("capture.mkv"), ORIGIN)
    assert events.count("controller-shutdown") == 1


def test_record_manual_and_disabled_do_not_enqueue() -> None:
    # Non-automatic policies still preserve a successful recording without publication work.
    for mode in (PublicationMode.MANUAL, PublicationMode.DISABLED):
        result, events = _run_record(mode, True)
        assert result == 0
        assert events == ["loop-quit", "controller-shutdown"]


def test_record_cancellation_remains_nonzero() -> None:
    # Portal or manual cancellation without a completed result must not look successful.
    result, events = _run_record(PublicationMode.AUTOMATIC, False)

    assert result == 1 and events == ["loop-quit", "controller-shutdown"]
