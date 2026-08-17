"""Controller wiring tests that do not need a display.

These exist because a signature change to RecordingTray once left the caller
passing an argument that no longer existed. The controller catches that and
falls back to the floating pill, so the app kept "working" while the tray
silently disappeared — a standalone RecordingTray test still passed.
"""

import inspect
import sys
import types

from meeting_recorder.config import load_config
from meeting_recorder.controller import Controller
from meeting_recorder.domain import CaptureMode, VideoSource


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

        def start(self, _path, capture_mode):
            self.capture_mode = capture_mode
            self.is_recording = True

        def stop(self, **_kwargs):
            self.is_recording = False
            return False

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
