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

    def enrich(completed):
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
        def prompt_capture_mode(self, _app_name, _timeout, **callbacks):
            self.callbacks = callbacks

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

    assert recorder.capture_mode is CaptureMode.AUDIO_VIDEO
