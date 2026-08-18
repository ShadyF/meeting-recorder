"""Configuration compatibility tests for the canonical video-source key."""

import json
import os
import errno
import tempfile
from unittest.mock import patch
from pathlib import Path

from meeting_recorder.calendar_oauth import CalendarOAuth
from meeting_recorder.config import (
    load_config, load_raw_config, save_google_calendar_ids, save_user_config,
    validate_google_calendar_ids,
)
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


def test_google_calendar_ids_are_opaque_deduped_and_save_only_that_key():
    assert validate_google_calendar_ids(["a/b", "a/b", "two"]) == ("a/b", "two")
    def check(path):
        save_google_calendar_ids(["first", "second"])
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["google_calendar_ids"] == ["first", "second"]
        assert saved["future_setting"] == "kept"
    _with_user_config({"future_setting": "kept"}, check)


def test_google_calendar_id_validation_enforces_list_count_type_and_length_limits():
    invalid = (None, "one", ["x"] * 51, [""], [1], ["x" * 1025])
    for value in invalid:
        try:
            validate_google_calendar_ids(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid ID selection accepted: {value!r}")


def test_atomic_config_failures_preserve_complete_previous_selection_and_remove_temp_files():
    def check(path):
        original = {"future_setting": "old", "google_calendar_ids": ["prior"]}
        path.write_text(json.dumps(original), encoding="utf-8")
        with patch("meeting_recorder.config.os.replace", side_effect=OSError("replace failed")):
            try:
                save_user_config({"future_setting": "new", "google_calendar_ids": ["new"]})
            except OSError:
                pass
            else:
                raise AssertionError("replace failure was hidden")
        assert json.loads(path.read_text(encoding="utf-8")) == original
        assert not list(path.parent.glob(".config-*.tmp"))

        with patch("meeting_recorder.config.os.fdopen", side_effect=OSError("write failed")):
            try:
                save_user_config({"google_calendar_ids": ["new"]})
            except OSError:
                pass
            else:
                raise AssertionError("write failure was hidden")
        assert json.loads(path.read_text(encoding="utf-8")) == original
        assert not list(path.parent.glob(".config-*.tmp"))

    _with_user_config({}, check)


def test_atomic_config_sets_private_mode_and_attempts_directory_fsync():
    attempted = []

    def check(path):
        with patch("meeting_recorder.config._fsync_directory", side_effect=attempted.append):
            save_user_config({"google_calendar_ids": ["one"]})
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert attempted == [path.parent]
        assert not list(path.parent.glob(".config-*.tmp"))

    import stat
    _with_user_config({}, check)


def test_config_directory_fsync_propagates_real_io_failure_and_accepts_supported_success():
    from meeting_recorder.config import _fsync_directory

    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        with patch("meeting_recorder.config.os.fsync", side_effect=OSError(errno.EIO, "disk")):
            try:
                _fsync_directory(directory)
            except OSError as exc:
                assert exc.errno == errno.EIO
            else:
                raise AssertionError("durability failure was hidden")
        calls = []
        with patch("meeting_recorder.config.os.fsync", side_effect=lambda fd: calls.append(fd)):
            _fsync_directory(directory)
        assert calls
