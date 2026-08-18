"""Calendar parser tests keep the credential CLI intentionally narrow."""

import io
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from meeting_recorder.__main__ import _cmd_calendar, build_parser
from meeting_recorder.calendar_oauth import CalendarAuthorizationDeniedError


def test_calendar_parser_accepts_only_the_three_credential_actions():
    parser = build_parser()
    for action in ("connect", "status", "disconnect"):
        args = parser.parse_args(["calendar", action])
        assert args.command == "calendar"
        assert args.calendar_command == action


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
