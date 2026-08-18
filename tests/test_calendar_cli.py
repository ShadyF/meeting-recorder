"""Calendar parser tests keep the credential CLI intentionally narrow."""

import io
import sys
import types
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from meeting_recorder.__main__ import _cmd_calendar, build_parser
from meeting_recorder.calendar_domain import CalendarInfo
from meeting_recorder.calendar_oauth import CalendarAuthorizationDeniedError
from meeting_recorder.calendar_refresh import CalendarRefreshReport, CalendarRefreshResult


def test_calendar_parser_accepts_calendar_actions_and_rejects_unsafe_selection_shapes():
    parser = build_parser()
    for action in ("connect", "status", "disconnect", "list", "select", "refresh"):
        arguments = ["calendar", action]
        if action == "select":
            arguments.extend(("--id", "one"))
        args = parser.parse_args(arguments)
        assert args.command == "calendar"
        assert args.calendar_command == action
    for arguments in (("calendar", "select"), ("calendar", "select", "--id", "one", "--clear")):
        try:
            parser.parse_args(arguments)
        except SystemExit:
            pass
        else:
            raise AssertionError("invalid selection shape accepted")


def test_calendar_parser_rejects_secret_token_and_code_options():
    parser = build_parser()
    for option in ("--client-secret", "--token", "--refresh-token", "--code"):
        try:
            parser.parse_args(["calendar", "connect", option, "value"])
        except SystemExit:
            pass
        else:
            raise AssertionError(f"forbidden option accepted: {option}")


def test_calendar_cli_reports_authorization_denial_without_callback_values():
    output = io.StringIO()
    cfg = SimpleNamespace()
    denied = CalendarAuthorizationDeniedError("state=private&error=access_denied")
    with patch("meeting_recorder.calendar_oauth.CalendarOAuth.connect", side_effect=denied):
        with redirect_stdout(output), redirect_stderr(output):
            assert _cmd_calendar(cfg, "connect") == 1
    message = output.getvalue().lower()
    assert "cancelled or denied" in message
    assert "misconfigured" not in message
    assert "state=" not in message and "access_denied" not in message


def test_calendar_select_dedupes_and_persists_caller_order_only_after_access_check():
    saved, output = [], io.StringIO()

    class Client:
        def __init__(self, _token): pass
        def list_calendars(self): return [CalendarInfo("second", None), CalendarInfo("first", None)]

    with patch("meeting_recorder.config.load_raw_config", return_value={}), \
         patch("meeting_recorder.config.save_google_calendar_ids", side_effect=saved.append), \
         patch("meeting_recorder.calendar_google.GoogleCalendarClient", Client), \
         patch("meeting_recorder.calendar_oauth.CalendarOAuth.access_token", return_value="token"), \
         redirect_stdout(output):
        assert _cmd_calendar(SimpleNamespace(), "select", ["second", "first", "second"]) == 0
    assert saved == [["second", "first"]] and "saved" in output.getvalue()


def test_calendar_select_inaccessible_id_and_clear_do_not_do_unneeded_oauth_work():
    saved, calls = [], []

    class Client:
        def __init__(self, _token): calls.append("client")
        def list_calendars(self): return [CalendarInfo("visible", None)]

    with patch("meeting_recorder.config.load_raw_config", return_value={}), \
         patch("meeting_recorder.config.save_google_calendar_ids", side_effect=saved.append), \
         patch("meeting_recorder.calendar_google.GoogleCalendarClient", Client), \
         patch("meeting_recorder.calendar_oauth.CalendarOAuth.access_token", return_value="token"):
        assert _cmd_calendar(SimpleNamespace(), "select", ["hidden"]) == 1
        assert not saved and calls == ["client"]
        assert _cmd_calendar(SimpleNamespace(), "select", clear=True) == 0
    assert saved == [[]] and calls == ["client"]


def test_calendar_list_escapes_control_characters_and_marks_explicit_selection():
    output = io.StringIO()

    class Client:
        def __init__(self, _token): pass
        def list_calendars(self): return [CalendarInfo("id\x01", "name\n", primary=True)]

    with patch("meeting_recorder.config.load_raw_config", return_value={"google_calendar_ids": ["id\x01"]}), \
         patch("meeting_recorder.calendar_google.GoogleCalendarClient", Client), \
         patch("meeting_recorder.calendar_oauth.CalendarOAuth.access_token", return_value="token"), \
         redirect_stdout(output):
        assert _cmd_calendar(SimpleNamespace(), "list") == 0
    assert '"id\\u0001" "name\\n" selected' in output.getvalue()


def test_calendar_refresh_empty_and_partial_results_have_correct_exit_codes_without_leaking_details():
    output = io.StringIO()
    with patch("meeting_recorder.config.load_raw_config", return_value={"google_calendar_ids": []}), redirect_stdout(output):
        assert _cmd_calendar(SimpleNamespace(), "refresh") == 0
    assert "no calendars selected" in output.getvalue()

    class Refresher:
        def __init__(self, *_args): pass
        def refresh(self, _ids, *, blocking):
            assert blocking
            return CalendarRefreshReport((CalendarRefreshResult("ok", True),
                                          CalendarRefreshResult("bad\x01", False, detail="unavailable")))

    output = io.StringIO()
    with patch("meeting_recorder.config.load_raw_config", return_value={"google_calendar_ids": ["ok", "bad\x01"]}), \
         patch("meeting_recorder.calendar_refresh.CalendarRefresher", Refresher), \
         redirect_stdout(output):
        assert _cmd_calendar(SimpleNamespace(), "refresh") == 1
    assert '"bad\\u0001" unavailable' in output.getvalue()


def test_calendar_config_write_oserror_returns_one_without_traceback():
    output = io.StringIO()
    with patch("meeting_recorder.config.load_raw_config", return_value={}), \
         patch("meeting_recorder.config.save_google_calendar_ids", side_effect=OSError("read-only")), \
         redirect_stdout(output), redirect_stderr(output):
        assert _cmd_calendar(SimpleNamespace(), "select", clear=True) == 1
    assert "misconfigured" in output.getvalue() and "traceback" not in output.getvalue().lower()


def test_run_starts_recorder_when_calendar_configuration_is_bad_and_shuts_down_once():
    events, signal_handlers = [], []

    class Loop:
        def run(self): signal_handlers[0]()
        def quit(self): events.append("loop-quit")

    class GLib:
        PRIORITY_HIGH = 1
        MainLoop = Loop
        @staticmethod
        def timeout_add(*_args): return 1
        @staticmethod
        def unix_signal_add(_priority, _signal, callback): signal_handlers.append(callback)

    class Recorder:
        def __init__(self, _cfg): events.append("recorder")

    class Controller:
        def __init__(self, *_args): events.append("controller")
        def on_meeting_start(self): pass
        def on_meeting_stop(self): pass
        def shutdown(self): events.append("shutdown")

    class Detector:
        def __init__(self, **_kwargs): events.append("detector")
        def tick(self): return True

    class Service:
        def __init__(self, *_args): events.append("service")
        def start(self): events.append("service-start")
        def stop(self, _timeout): events.append("service-stop"); return True

    cfg = SimpleNamespace(allowlist=(), poll_interval_seconds=1, start_debounce_seconds=1,
                          stop_debounce_seconds=1, google_calendar_client_id="bad", google_calendar_loopback_port=True)
    gi = types.ModuleType("gi")
    gi.require_version = lambda *_args: None
    repository = types.ModuleType("gi.repository")
    repository.GLib = GLib
    with patch.dict(sys.modules, {"gi": gi, "gi.repository": repository}), \
         patch("meeting_recorder.notifier.Notifier", lambda: object()), \
         patch("meeting_recorder.recorder.Recorder", Recorder), \
         patch("meeting_recorder.controller.Controller", Controller), \
         patch("meeting_recorder.detector.MeetingDetector", Detector), \
         patch("meeting_recorder.calendar_service.CalendarRefreshService", Service), \
         patch("meeting_recorder.calendar_oauth.CalendarOAuth.access_token", side_effect=AssertionError("network")):
        from meeting_recorder.__main__ import _cmd_run
        assert _cmd_run(cfg) == 0
    assert events.count("recorder") == events.count("detector") == events.count("service-start") == 1
    assert events.count("service-stop") == events.count("shutdown") == 1
