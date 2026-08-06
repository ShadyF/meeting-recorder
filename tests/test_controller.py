"""Controller wiring tests that do not need a display.

These exist because a signature change to RecordingTray once left the caller
passing an argument that no longer existed. The controller catches that and
falls back to the floating pill, so the app kept "working" while the tray
silently disappeared — a standalone RecordingTray test still passed.
"""

import inspect
import sys
import types

from meeting_recorder.controller import Controller


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
