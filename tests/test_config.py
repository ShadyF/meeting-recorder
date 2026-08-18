"""Configuration compatibility tests for the canonical video-source key."""

import json
import os
import tempfile
from pathlib import Path

from meeting_recorder.config import load_config, load_raw_config, save_user_config
from meeting_recorder.domain import VideoSource


def _with_user_config(data, check):
    """Run a check against an isolated user configuration file."""
    previous = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as temp:
        os.environ["XDG_CONFIG_HOME"] = temp
        path = Path(temp) / "meeting-recorder" / "config.json"
        path.parent.mkdir()
        path.write_text(json.dumps(data), encoding="utf-8")
        try:
            check(path)
        finally:
            if previous is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = previous


def test_legacy_capture_mode_loads_as_video_source_for_typed_and_raw_config():
    def check(_path):
        assert load_config().video_source is VideoSource.AREA
        raw = load_raw_config()
        assert raw["video_source"] == "area"
        assert "capture_mode" not in raw

    _with_user_config({"capture_mode": "area"}, check)


def test_canonical_video_source_wins_over_legacy_capture_mode():
    def check(_path):
        assert load_config().video_source is VideoSource.WINDOW
        assert load_raw_config()["video_source"] == "window"

    _with_user_config({"capture_mode": "area", "video_source": "window"}, check)


def test_invalid_legacy_or_canonical_source_falls_back_in_typed_config():
    def check(_path):
        assert load_config().video_source is VideoSource.FULLSCREEN
        assert load_raw_config()["video_source"] == "unexpected"

    _with_user_config({"capture_mode": "unexpected"}, check)
    _with_user_config({"video_source": "unexpected"}, check)


def test_saving_config_omits_legacy_capture_mode():
    def check(path):
        save_user_config({"capture_mode": "area", "video_source": "window"})
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["video_source"] == "window"
        assert "capture_mode" not in saved

    _with_user_config({}, check)


def test_google_client_environment_override_is_typed_only_and_not_saved():
    previous = os.environ.get("MEETING_RECORDER_GOOGLE_CLIENT_ID")
    os.environ["MEETING_RECORDER_GOOGLE_CLIENT_ID"] = "env.apps.googleusercontent.com"
    try:
        def check(path):
            assert load_config().google_calendar_client_id == "env.apps.googleusercontent.com"
            assert load_raw_config()["google_calendar_client_id"] == "file.apps.googleusercontent.com"
            save_user_config(load_raw_config())
            assert "env.apps.googleusercontent.com" not in path.read_text(encoding="utf-8")

        _with_user_config({"google_calendar_client_id": "file.apps.googleusercontent.com"}, check)
    finally:
        if previous is None:
            os.environ.pop("MEETING_RECORDER_GOOGLE_CLIENT_ID", None)
        else:
            os.environ["MEETING_RECORDER_GOOGLE_CLIENT_ID"] = previous
