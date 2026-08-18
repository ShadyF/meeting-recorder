"""Calendar parser tests keep the credential CLI intentionally narrow."""

from meeting_recorder.__main__ import build_parser


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
