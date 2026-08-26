"""Configuration compatibility tests for the canonical video-source key."""

import json
import os
import errno
import tempfile
from unittest.mock import patch
from pathlib import Path

from meeting_recorder.calendar_oauth import CalendarOAuth
from meeting_recorder.config import (
    Config, PublicationMode, load_config, load_defaults, load_raw_config,
    require_speakr_token, resolve_speakr_url, save_google_calendar_ids, save_user_config,
    validate_google_calendar_ids, validate_speakr_allowed_ssids,
)
from meeting_recorder.domain import VideoSource
from meeting_recorder.speakr_domain import normalize_speakr_url


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


def test_speakr_defaults_and_publication_modes_are_typed():
    defaults = load_defaults()
    config = load_config_for_test({})

    assert defaults["speakr_publication_mode"] == "disabled"
    assert defaults["speakr_allowed_ssids"] == []
    assert config.speakr_publication_mode is PublicationMode.DISABLED
    assert config.speakr_allowed_ssids == ()
    assert config.speakr_allowed_ssid_bytes == ()
    assert [mode.value for mode in PublicationMode] == ["disabled", "manual", "automatic"]

    # Each supported spelling maps to its corresponding typed policy.
    for mode in PublicationMode:
        assert load_config_for_test({"speakr_publication_mode": mode.value}).speakr_publication_mode is mode


def load_config_for_test(overrides):
    """Build a typed config from shipped defaults without touching user files."""
    data = load_defaults()
    data.update(overrides)
    return Config.from_dict(data)


def test_legacy_config_without_speakr_keys_uses_shipped_defaults():
    def check(_path):
        config = load_config()
        assert config.speakr_publication_mode is PublicationMode.DISABLED
        assert config.speakr_allowed_ssids == ()

    _with_user_config({"future_setting": "kept"}, check)


def test_invalid_publication_mode_and_legacy_missing_keys_are_safe():
    config = load_config_for_test({"speakr_publication_mode": "unexpected"})

    assert config.speakr_publication_mode is PublicationMode.DISABLED
    assert config.speakr_allowed_ssids == ()


def test_speakr_ssids_preserve_exact_text_and_project_to_network_bytes():
    ssids = [" Cafe ", "café", "CAFE"]
    config = load_config_for_test({"speakr_allowed_ssids": ssids})

    assert config.speakr_allowed_ssids == tuple(ssids)
    assert config.speakr_allowed_ssid_bytes == tuple(ssid.encode("utf-8") for ssid in ssids)


def test_invalid_speakr_ssid_lists_fail_closed():
    invalid = (None, "Cafe", [""], ["Cafe", "Cafe"], ["Cafe", 7],
               ["Cafe\x00wifi"], ["Cafe\nwifi"], ["é" * 17])

    # Validation rejects every invalid list instead of partially admitting it.
    for value in invalid:
        try:
            validate_speakr_allowed_ssids(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid SSID list accepted: {value!r}")

    for value in invalid:
        assert load_config_for_test({"speakr_allowed_ssids": value}).speakr_allowed_ssids == ()


def test_production_speakr_resolver_requires_https_but_normalizer_keeps_http():
    https_config = load_config_for_test({"speakr_url": "https://Example.com/"})
    assert resolve_speakr_url(https_config, {}) == "https://example.com"

    # The low-level transport normalizer still supports local HTTP fake servers.
    assert normalize_speakr_url("http://127.0.0.1:8080/") == "http://127.0.0.1:8080"
    for value in ("http://127.0.0.1:8080/", None, ""):
        config = load_config_for_test({"speakr_url": value})
        try:
            resolve_speakr_url(config, {})
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe Speakr URL accepted: {value!r}")


def test_speakr_token_uses_environment_before_the_fixed_container_secret():
    # Isolate the fixed container secret source without reading any real credential.
    with patch("meeting_recorder.config._SPEAKR_TOKEN_SECRET_PATH") as secret_path:
        # Prove the explicit environment token wins over the fixed secret fallback.
        secret_path.read_text.return_value = "secret-token"
        assert require_speakr_token({}) == "secret-token"
        assert require_speakr_token({"MEETING_RECORDER_SPEAKR_TOKEN": "env-token"}) == "env-token"
        assert not secret_path.read_text.called or secret_path.read_text.call_count == 1

        # Validate both sources with the same existing token restrictions.
        secret_path.read_text.return_value = "bad token"
        for values in ({}, {"MEETING_RECORDER_SPEAKR_TOKEN": "bad token"}):
            try:
                require_speakr_token(values)
            except ValueError:
                pass
            else:
                raise AssertionError("invalid Speakr token was accepted")


def test_speakr_publication_keys_roundtrip_and_credentials_are_scrubbed():
    def check(path):
        raw = load_raw_config()
        assert raw["speakr_publication_mode"] == "automatic"
        assert raw["speakr_allowed_ssids"] == ["Office WiFi"]
        raw["speakr_token"] = "must-not-be-saved"
        # Model the GUI changing only curated visible fields before saving.
        raw.update({"container": "mp4", "record_screen": False})
        save_user_config(raw)
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["speakr_publication_mode"] == "automatic"
        assert saved["speakr_allowed_ssids"] == ["Office WiFi"]
        assert "speakr_token" not in saved

    _with_user_config({"speakr_publication_mode": "automatic",
                       "speakr_allowed_ssids": ["Office WiFi"]}, check)


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
