"""Settings application tests for native and managed service supervisors."""

import sys
import types
from typing import cast
from unittest.mock import patch


gi = types.ModuleType("gi")
setattr(gi, "require_version", lambda *_args: None)
repository = types.ModuleType("gi.repository")
setattr(repository, "Gtk", types.SimpleNamespace(Window=object))

# Load the settings module without requiring desktop GI packages in CI.
with patch.dict(sys.modules, {"gi": gi, "gi.repository": repository}):
    from meeting_recorder import settings_gui


def test_managed_settings_restart_signals_container_pid_one() -> None:
    """Use the container supervisor instead of trying to reach host systemd."""
    # Exercise the managed path while preventing a real container signal.
    with patch.dict(settings_gui.os.environ, {"MEETING_RECORDER_MANAGED_CONTAINER": "1"}), \
            patch.object(settings_gui.os, "kill") as kill, \
            patch.object(settings_gui.subprocess, "run") as run:
        assert settings_gui._restart_service()
    kill.assert_called_once_with(1, settings_gui.signal.SIGTERM)
    run.assert_not_called()


def test_managed_settings_restart_failure_is_reported_without_systemctl() -> None:
    """Keep the settings window open when the container cannot be signalled."""
    # Keep the managed path isolated while simulating a denied PID 1 signal.
    with patch.dict(settings_gui.os.environ, {"MEETING_RECORDER_MANAGED_CONTAINER": "1"}), \
            patch.object(settings_gui.os, "kill", side_effect=OSError("denied")), \
            patch.object(settings_gui.subprocess, "run") as run:
        assert not settings_gui._restart_service()
    run.assert_not_called()


class _Widget:
    """Return a fixed widget value through the GTK getter used by Save."""

    def __init__(self, value) -> None:
        self.value = value

    def get_filename(self):
        return self.value

    def get_active(self):
        return self.value

    def get_text(self):
        return self.value

    def get_value(self):
        return self.value


class _Status:
    """Record the status message shown by a lightweight settings window stub."""

    def __init__(self) -> None:
        self.markup = ""

    def set_markup(self, markup: str) -> None:
        # Keep the latest visible message for assertions.
        self.markup = markup


class _Window:
    """Provide only the widget state consumed by SettingsWindow._save."""

    def __init__(self) -> None:
        self.data = {"output_dir": "~/Videos/MeetingRecorder"}
        self.output_chooser = _Widget("/tmp/recordings")
        self.format_combo = _Widget(1)
        self.screen_switch = _Widget(True)
        self.video_source_combo = _Widget(0)
        self.region_entry = _Widget(" 1,2,3,4 ")
        self.cursor_switch = _Widget(True)
        self.mic_switch = _Widget(True)
        self.sys_switch = _Widget(False)
        self.noise_switch = _Widget(True)
        self.mic_vol = _Widget(1.25)
        self.sys_vol = _Widget(0.75)
        self.auto_switch = _Widget(False)
        self.stop_spin = _Widget(4.5)
        self.status = _Status()
        self.closed = False

    def close(self) -> None:
        # Record the close request without creating a GTK window.
        self.closed = True


def test_save_in_managed_container_persists_signals_and_closes_window() -> None:
    """Close only after the saved settings successfully signal container PID 1."""
    window = _Window()
    saved = []

    # Save the updated data and use the real managed restart path with a harmless signal stub.
    with patch.dict(settings_gui.os.environ, {"MEETING_RECORDER_MANAGED_CONTAINER": "1"}), \
            patch.object(settings_gui, "save_user_config", side_effect=lambda data: saved.append(dict(data))), \
            patch.object(settings_gui.os, "kill") as kill:
        settings_gui.SettingsWindow._save(cast(settings_gui.SettingsWindow, window))

    # Confirm persistence, the supervisor signal, and the managed-window lifecycle.
    assert saved == [window.data]
    assert window.data["container"] == "mp4"
    assert window.data["capture_region"] == "1,2,3,4"
    kill.assert_called_once_with(1, settings_gui.signal.SIGTERM)
    assert window.closed and "Saved and applied" in window.status.markup


def test_save_in_managed_container_keeps_window_open_when_signal_fails() -> None:
    """Show that a saved setting is not applied when the PID 1 signal fails."""
    window = _Window()
    saved = []

    # Preserve the saved file while making the container restart request fail.
    with patch.dict(settings_gui.os.environ, {"MEETING_RECORDER_MANAGED_CONTAINER": "1"}), \
            patch.object(settings_gui, "save_user_config", side_effect=lambda data: saved.append(dict(data))), \
            patch.object(settings_gui.os, "kill", side_effect=OSError("denied")):
        settings_gui.SettingsWindow._save(cast(settings_gui.SettingsWindow, window))

    # Keep the window open and report that the saved values are still inactive.
    assert saved == [window.data]
    assert not window.closed
    assert "not applied" in window.status.markup
