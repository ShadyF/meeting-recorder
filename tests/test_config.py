"""Configuration compatibility tests for the canonical video-source key."""

import json
import os
import tempfile
from pathlib import Path

from meeting_recorder.calendar_oauth import CalendarOAuth
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


def test_empty_google_client_environment_override_stays_calendar_only_invalid_state():
    previous = os.environ.get("MEETING_RECORDER_GOOGLE_CLIENT_ID")
    os.environ["MEETING_RECORDER_GOOGLE_CLIENT_ID"] = ""
    try:
        def check(path):
            cfg = load_config()
            assert cfg.google_calendar_client_id is None
            assert cfg.video_source is VideoSource.FULLSCREEN
            assert CalendarOAuth(cfg).status().state == "misconfigured"
            assert load_raw_config()["google_calendar_client_id"] == "file.apps.googleusercontent.com"
            save_user_config(load_raw_config())
            saved = json.loads(path.read_text(encoding="utf-8"))
            assert saved["google_calendar_client_id"] == "file.apps.googleusercontent.com"

        _with_user_config({"google_calendar_client_id": "file.apps.googleusercontent.com"}, check)
    finally:
        if previous is None:
            os.environ.pop("MEETING_RECORDER_GOOGLE_CLIENT_ID", None)
        else:
            os.environ["MEETING_RECORDER_GOOGLE_CLIENT_ID"] = previous


def test_calendar_credential_shaped_keys_are_not_retained_but_unknown_keys_are():
    forbidden = {
        "access_token": "access",
        "authorization_code": "code",
        "client_secret": "secret",
        "credential_json": "{}",
        "google_calendar_client_secret": "secret",
        "google_calendar_credential_json": "{}",
        "google_calendar_refresh_token": "refresh",
        "google_calendar_access_token": "access",
        "google_calendar_authorization_code": "code",
        "refresh_token": "refresh",
    }

    def check(path):
        raw = load_raw_config()
        assert not (set(forbidden) & set(raw))
        assert raw["future_setting"] == "kept"
        save_user_config(raw)
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert not (set(forbidden) & set(saved))
        assert saved["future_setting"] == "kept"

    _with_user_config({**forbidden, "future_setting": "kept"}, check)


def test_downloaded_google_credential_document_is_scrubbed_without_losing_unknown_data():
    downloaded = {
        "client_id": "12345-example.apps.googleusercontent.com",
        "project_id": "owned-project",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": "desktop-secret",
        "redirect_uris": ["http://localhost"],
    }

    def check(path):
        raw = load_raw_config()
        assert "installed" not in raw
        assert raw["future_setting"] == "kept"
        assert raw["web"] == {"layout": "unrelated"}
        save_user_config(raw)
        saved = path.read_text(encoding="utf-8")
        assert "installed" not in saved and "desktop-secret" not in saved
        assert json.loads(saved)["web"] == {"layout": "unrelated"}

    _with_user_config({"installed": downloaded, "web": {"layout": "unrelated"},
                       "future_setting": "kept"}, check)


def test_web_credential_document_is_scrubbed_while_unrelated_installed_value_survives():
    downloaded = {
        "client_id": "12345-example.apps.googleusercontent.com",
        "client_secret": "web-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }

    def check(path):
        raw = load_raw_config()
        assert "web" not in raw
        assert raw["installed"] == {"layout": "unrelated"}
        save_user_config(raw)
        saved = path.read_text(encoding="utf-8")
        assert "web-secret" not in saved
        assert json.loads(saved)["installed"] == {"layout": "unrelated"}

    _with_user_config({"web": downloaded, "installed": {"layout": "unrelated"}}, check)
