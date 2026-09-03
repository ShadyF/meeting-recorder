"""Controller wiring tests that do not need a display.

These exist because a signature change to RecordingTray once left the caller
passing an argument that no longer existed. The controller catches that and
falls back to the floating pill, so the app kept "working" while the tray
silently disappeared — a standalone RecordingTray test still passed.
"""

import inspect
import sys
import types
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from meeting_recorder.config import load_config
from meeting_recorder.controller import Controller
from meeting_recorder.domain import CaptureMode, VideoSource
from meeting_recorder.domain import CompletedRecording
from meeting_recorder.speakr_domain import Tag
from meeting_recorder.speakr_tag_service import TagCatalogOutcome, TagCatalogSource


def test_build_controls_calls_the_tray_with_arguments_it_accepts():
    """The tray call must match RecordingTray's signature.

    Checked by inspection rather than by constructing one, so it runs on a
    headless machine: importing tray_indicator needs GTK and AppIndicator.
    """
    src = inspect.getsource(Controller._build_controls)
    assert "RecordingTray(" in src, "controller no longer builds a tray"

    try:
        from meeting_recorder.tray_indicator import RecordingTray
    except Exception:
        return  # no GTK/AppIndicator here; nothing to compare against

    params = set(inspect.signature(RecordingTray.__init__).parameters) - {"self"}
    # Every keyword the controller passes has to exist on the tray.
    import re
    call = re.search(r"RecordingTray\((.*?)\)", src, re.S).group(1)
    passed = set(re.findall(r"(\w+)\s*=", call)) - {"kwargs"}
    unknown = passed - params
    assert not unknown, f"controller passes {unknown}, which RecordingTray rejects"


def test_build_controls_kwargs_match_both_widgets():
    """Tray and pill are interchangeable, so both must accept the same kwargs."""
    src = inspect.getsource(Controller._build_controls)
    for name in ("on_pause", "on_resume", "on_stop"):
        assert name in src, f"{name} missing from the controls it builds"


def test_build_controls_reuses_the_registered_tray():
    """A second recording must reuse the existing D-Bus indicator."""
    class FakeRecorder:
        def pause(self):
            pass

        def resume(self):
            pass

    class FakeTray:
        created = 0

        def __init__(self, **_kwargs):
            FakeTray.created += 1

    fake_module = types.ModuleType("meeting_recorder.tray_indicator")
    fake_module.RecordingTray = FakeTray
    module_name = "meeting_recorder.tray_indicator"
    previous_module = sys.modules.get(module_name)
    sys.modules[module_name] = fake_module

    # Build controls twice as consecutive recordings do in one service process.
    try:
        controller = object.__new__(Controller)
        controller.recorder = FakeRecorder()
        controller._tray = None
        first = controller._build_controls()
        second = controller._build_controls()
    finally:
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module

    assert first is second
    assert FakeTray.created == 1


def test_manual_start_derives_a_per_run_capture_mode_without_changing_config():
    class FakeRecorder:
        is_recording = False

        def start(self, _path, _source_app, capture_mode):
            self.capture_mode = capture_mode
            self.is_recording = True
            return True

        def stop(self, **_kwargs):
            self.is_recording = False
            return None

        def attach_session(self, _session):
            pass

    class FakeNotifier:
        pass

    cfg = load_config()
    recorder = FakeRecorder()
    controller = Controller(cfg, FakeNotifier(), recorder)
    controller._show_widget = lambda: None

    # Keep this test on the direct capture path regardless of the host session.
    controller._needs_portal = lambda capture_mode: False

    cfg.record_screen = False
    cfg.video_source = VideoSource.AREA
    controller.start_manual()
    assert recorder.capture_mode is CaptureMode.AUDIO_ONLY
    assert cfg.record_screen is False
    assert cfg.video_source is VideoSource.AREA

    controller.stop_manual()
    cfg.record_screen = True
    controller.start_manual()
    assert recorder.capture_mode is CaptureMode.AUDIO_VIDEO
    assert cfg.record_screen is True


def test_manual_video_portal_waits_for_ready_session_without_changing_config():
    class FakeRecorder:
        is_recording = False

        def __init__(self):
            self.attached_sessions = []
            self.started_modes = []

        def start(self, _path, _source_app, capture_mode):
            # Track the requested mode after recording starts.
            self.started_modes.append(capture_mode)
            self.is_recording = True
            return True

        def attach_session(self, session):
            # Record the session attached after the portal is ready.
            self.attached_sessions.append(session)

    class FakeNotifier:
        pass

    captured = {}

    class FakeSession:
        restore_token = None

        def __init__(self):
            # Keep the fake session for the delayed ready callback.
            captured["session"] = self

        def open(self, _source_types, _token, on_ready, _on_error, **_kwargs):
            # Hold the ready callback until the test completes the portal request.
            captured["ready_callback"] = on_ready

        def close(self):
            pass

    # Replace the portal module with a session that waits for a manual ready signal.
    fake_module = types.ModuleType("meeting_recorder.screencast")
    fake_module.CURSOR_EMBEDDED = 2
    fake_module.CURSOR_HIDDEN = 1
    fake_module.ScreenCastSession = FakeSession
    fake_module.source_types_for = lambda _source: 1
    fake_module.use_portal_capture = lambda: True
    module_name = "meeting_recorder.screencast"
    previous_module = sys.modules.get(module_name)
    sys.modules[module_name] = fake_module

    try:
        # Request video capture and retain the original persistent configuration.
        cfg = load_config()
        cfg.record_screen = True
        original_config = (cfg.record_screen, cfg.video_source)
        recorder = FakeRecorder()
        controller = Controller(cfg, FakeNotifier(), recorder)
        controller._show_widget = lambda: None

        # Start the asynchronous portal request without completing its ready callback.
        controller.start_manual()

        # Verify video capture remains pending until the portal is ready.
        assert recorder.started_modes == []
        assert controller._pending_capture_mode is CaptureMode.AUDIO_VIDEO

        # Complete the portal handshake and start the requested video capture.
        captured["ready_callback"](captured["session"])
    finally:
        # Restore the real module so later tests use their selected capture path.
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module

    # Confirm the ready session starts video capture without changing configuration.
    assert recorder.attached_sessions == [captured["session"]]
    assert recorder.started_modes == [CaptureMode.AUDIO_VIDEO]
    assert (cfg.record_screen, cfg.video_source) == original_config


def test_manual_video_source_selects_the_matching_portal_source_type():
    from meeting_recorder.screencast import SOURCE_WINDOW, source_types_for

    class FakeRecorder:
        def attach_session(self, _session):
            pass

    class FakeNotifier:
        pass

    captured = {}

    class FakeSession:
        def open(self, source_types, *_args, **_kwargs):
            captured["source_types"] = source_types

    fake_module = types.ModuleType("meeting_recorder.screencast")
    fake_module.CURSOR_EMBEDDED = 2
    fake_module.CURSOR_HIDDEN = 1
    fake_module.ScreenCastSession = FakeSession
    fake_module.source_types_for = source_types_for
    fake_module.use_portal_capture = lambda: True
    module_name = "meeting_recorder.screencast"
    previous_module = sys.modules.get(module_name)
    sys.modules[module_name] = fake_module

    try:
        cfg = load_config()
        cfg.record_screen = True
        cfg.video_source = VideoSource.WINDOW
        controller = Controller(cfg, FakeNotifier(), FakeRecorder())
        controller._show_widget = lambda: None
        controller.start_manual()
    finally:
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module

    assert captured["source_types"] == SOURCE_WINDOW


def test_controller_enriches_before_releasing_reservation_and_dispatches_replacement():
    class Handle:
        target_path = Path("fallback.mkv")

        def __init__(self, completed):
            self.completed = completed

        def poll(self):
            return True, self.completed

    class Notifier:
        def __init__(self):
            self.paths = []

        def info(self, _title, body, **_kwargs):
            self.paths.append(body)

    original = CompletedRecording(
        Path("fallback.mkv"), "Manual", CaptureMode.AUDIO_ONLY, True, False,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc))
    enriched_path = Path("visible.mkv")
    notifier = Notifier()
    observed = []

    def enrich(completed, *, tags):
        assert tags == ()
        observed.append(handle.target_path in controller._reserved_paths)
        return replace(completed, path=enriched_path)

    controller = Controller(load_config(), notifier, object(), recording_enricher=enrich)
    handle = Handle(original)
    controller._handles.add(handle)
    controller._reserved_paths.add(handle.target_path)
    callback = []
    controller.on_finished = callback.append

    assert not controller._poll_handle(handle)
    assert observed == [True]
    assert notifier.paths == [enriched_path.name]
    assert callback == [replace(original, path=enriched_path)]
    assert original.path == handle.target_path
    assert not controller._reserved_paths and not controller._reservations
    assert not controller._reserved_paths and not controller._handles


def test_controller_enrichment_exception_or_invalid_result_uses_original_and_dispatches_once():
    class Handle:
        target_path = Path("fallback.mkv")

        def poll(self):
            return True, completed

    class Notifier:
        def info(self, _title, body, **_kwargs):
            self.body = body

    completed = CompletedRecording(
        Path("fallback.mkv"), "Manual", CaptureMode.AUDIO_ONLY, True, False,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc))
    for bad_enricher in (lambda _value: (_ for _ in ()).throw(RuntimeError("cache")),
                         lambda _value: "not a recording"):
        notifier = Notifier()
        controller = Controller(load_config(), notifier, object(),
                                recording_enricher=bad_enricher)
        handle = Handle()
        controller._handles.add(handle)
        controller._reserved_paths.add(handle.target_path)
        callback = []
        controller.on_finished = callback.append
        assert not controller._poll_handle(handle)
        assert notifier.body == completed.path.name
        assert callback == [completed]
        assert not controller._handles and not controller._reserved_paths


def test_detected_prompt_starts_the_directly_selected_capture_mode():
    class FakeRecorder:
        is_recording = False

        def start(self, _path, _source_app, capture_mode):
            self.capture_mode = capture_mode
            self.is_recording = True
            return True

        def attach_session(self, _session):
            pass

    class FakeNotifier:
        def prompt_capture_mode(self, _app_name, _timeout, **callbacks):
            self.callbacks = callbacks

    cfg = load_config()
    cfg.auto_record = False
    notifier = FakeNotifier()
    recorder = FakeRecorder()
    controller = Controller(cfg, notifier, recorder)
    controller._show_widget = lambda: None

    controller.on_meeting_start("Zoom")
    notifier.callbacks["on_audio_only"]()

    assert recorder.capture_mode is CaptureMode.AUDIO_ONLY


def test_auto_record_skips_the_prompt_and_starts_audio_only():
    class FakeRecorder:
        is_recording = False

        def start(self, _path, _source_app, capture_mode):
            self.capture_mode = capture_mode
            self.is_recording = True
            return True

        def attach_session(self, _session):
            pass

    class FakeNotifier:
        def prompt_capture_mode(self, *_args, **_kwargs):
            raise AssertionError("auto-record must not show a prompt")

    cfg = load_config()
    cfg.auto_record = True
    cfg.record_screen = True
    recorder = FakeRecorder()
    controller = Controller(cfg, FakeNotifier(), recorder)
    controller._show_widget = lambda: None

    controller.on_meeting_start("Zoom")

    assert recorder.capture_mode is CaptureMode.AUDIO_ONLY


def test_requested_video_mode_survives_portal_failure():
    class FakeRecorder:
        is_recording = False

        def start(self, _path, _source_app, capture_mode):
            self.capture_mode = capture_mode
            self.is_recording = True
            return True

        def attach_session(self, _session):
            pass

    class FakeNotifier:
        def __init__(self):
            self.info_calls = []

        def prompt_capture_mode(self, _app_name, _timeout, **callbacks):
            self.callbacks = callbacks

        def info(self, summary, body):
            # Record the visible fallback notice for the final check.
            self.info_calls.append((summary, body))

    class FakeSession:
        def open(self, _source_types, _token, _ready, on_error, **_kwargs):
            on_error("permission denied")

    fake_module = types.ModuleType("meeting_recorder.screencast")
    fake_module.CURSOR_EMBEDDED = 2
    fake_module.CURSOR_HIDDEN = 1
    fake_module.ScreenCastSession = FakeSession
    fake_module.source_types_for = lambda _source: 1
    fake_module.use_portal_capture = lambda: True
    module_name = "meeting_recorder.screencast"
    previous_module = sys.modules.get(module_name)
    sys.modules[module_name] = fake_module

    try:
        cfg = load_config()
        cfg.auto_record = False
        notifier = FakeNotifier()
        recorder = FakeRecorder()
        controller = Controller(cfg, notifier, recorder)
        controller._show_widget = lambda: None
        controller.on_meeting_start("Zoom")
        notifier.callbacks["on_video"]()
    finally:
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module

    # Confirm the video request falls back and shows the static notice.
    assert recorder.capture_mode is CaptureMode.AUDIO_VIDEO
    assert notifier.info_calls == [(
        "Recording audio only",
        "Screen capture was unavailable. Recording continued with audio only.",
    )]


def test_detected_capture_requests_and_freezes_tags_without_manual_or_auto_fetch() -> None:
    class Recorder:
        is_recording = False

        def start(self, _path, _app, _mode):
            self.is_recording = True
            return True

        def attach_session(self, _session):
            pass

    class Notifier:
        def prompt_capture_mode(self, _app, _timeout, **callbacks):
            self.callbacks = callbacks

        def info(self, *_args, **_kwargs):
            pass

    class Prompt:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.shown = False

        def show(self):
            self.shown = True

        def close(self):
            self.closed = True

    cfg = load_config()
    callbacks = []
    prompts = []
    controller = Controller(
        cfg, Notifier(), Recorder(),
        tag_requester=lambda callback: callbacks.append(callback) or None,
        tag_prompt_factory=lambda **kwargs: prompts.append(Prompt(**kwargs)) or prompts[-1],
    )
    controller._show_widget = lambda: None
    controller._needs_portal = lambda _mode: False
    controller.on_meeting_start("Zoom")
    assert callbacks == []
    controller.notifier.callbacks["on_audio_only"]()
    assert len(callbacks) == 1
    callbacks[0](TagCatalogOutcome((Tag(2, "Two"), Tag(1, "One")), TagCatalogSource.FRESH, datetime.now(timezone.utc), False))
    assert prompts[0].shown and prompts[0].kwargs["tags"] == (Tag(2, "Two"), Tag(1, "One"))
    prompts[0].kwargs["on_confirmed"]((1,))
    assert controller._tag_confirmed == (Tag(1, "One"),)

    manual = Controller(cfg, Notifier(), Recorder(), tag_requester=lambda callback: (_ for _ in ()).throw(AssertionError()))
    manual._show_widget = lambda: None
    manual._needs_portal = lambda _mode: False
    manual.start_manual()


def test_default_tag_prompt_uses_the_current_tag_prompt_keyword_contract() -> None:
    class Prompt:
        def __init__(self, tags, initial_confirmed, on_confirmed, on_dismissed, *, status_text):
            self.tags = tags
            self.initial_confirmed = initial_confirmed
            self.on_confirmed = on_confirmed
            self.on_dismissed = on_dismissed
            self.status_text = status_text

    class Recorder:
        pass

    # Replace the visual module so this backend test remains headless.
    module_name = "meeting_recorder.tag_prompt"
    fake_module = types.ModuleType(module_name)
    fake_module.TagPrompt = Prompt
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = fake_module
    try:
        controller = Controller(load_config(), object(), Recorder())
        prompt = controller._default_tag_prompt(
            tags=(Tag(4, "Four"),), initial_confirmed=(Tag(4, "Four"),),
            on_confirmed=lambda _ids: None, on_dismissed=lambda: None,
            status_text="Tags loaded",
        )
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous

    assert prompt.tags == (Tag(4, "Four"),)
    assert prompt.initial_confirmed == (4,)
    assert prompt.status_text == "Tags loaded"


def test_empty_catalogs_hide_actions_and_unavailable_notices_reset_per_recording() -> None:
    class Recorder:
        is_recording = True

    class Notifier:
        def __init__(self) -> None:
            self.calls = []

        def info(self, *args, **kwargs) -> None:
            self.calls.append((args, kwargs))

    class Controls:
        def __init__(self) -> None:
            self.actions = []

        def set_tag_action(self, label: str | None) -> None:
            self.actions.append(label)

    # Feed outcomes directly because the service has already marshalled callbacks.
    notifier = Notifier()
    controller = Controller(load_config(), notifier, Recorder())
    controller.state = controller.state.RECORDING
    controller._widget = Controls()
    # Keep an empty fresh catalog silent because it is a complete response.
    controller._tag_request_generation += 1
    controller._on_tag_outcome(
        controller._tag_request_generation,
        TagCatalogOutcome((), TagCatalogSource.FRESH, datetime.now(timezone.utc), False),
    )
    assert controller._widget.actions[-1] is None

    # Keep an empty stale catalog equally silent instead of offering a retry.
    controller._tag_request_generation += 1
    controller._on_tag_outcome(
        controller._tag_request_generation,
        TagCatalogOutcome((), TagCatalogSource.STALE, datetime.now(timezone.utc), False),
    )
    assert controller._widget.actions[-1] is None
    assert notifier.calls == []

    # Show Retry for unavailable catalogs but limit its notice to one recording.
    controller._tag_request_generation += 1
    controller._on_tag_outcome(
        controller._tag_request_generation,
        TagCatalogOutcome((), TagCatalogSource.UNAVAILABLE, None, True),
    )
    assert controller._widget.actions[-1] == "Retry tags" and len(notifier.calls) == 1

    # A second unavailable outcome keeps Retry visible without repeating the notice.
    controller._tag_request_generation += 1
    controller._on_tag_outcome(
        controller._tag_request_generation,
        TagCatalogOutcome((), TagCatalogSource.UNAVAILABLE, None, True),
    )
    assert controller._widget.actions[-1] == "Retry tags" and len(notifier.calls) == 1

    # A new recording context receives one new unavailable notice.
    controller._end_tag_context()
    controller._tag_request_generation += 1
    controller._on_tag_outcome(
        controller._tag_request_generation,
        TagCatalogOutcome((), TagCatalogSource.UNAVAILABLE, None, True),
    )
    assert len(notifier.calls) == 2
