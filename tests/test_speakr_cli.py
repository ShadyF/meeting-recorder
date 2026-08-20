"""CLI and local-server acceptance tests for explicit Speakr publication."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from meeting_recorder.__main__ import _cmd_speakr_upload, build_parser
from meeting_recorder.calendar_domain import MeetingSnapshot, OccurrenceKey
from meeting_recorder.config import Config, require_speakr_token, resolve_speakr_url
from meeting_recorder.meeting_sidecar import MeetingSidecar, sidecar_path, write_sidecar
from meeting_recorder.speakr_domain import PublicationState
from meeting_recorder.speakr_http import StdlibSpeakrTransport
from meeting_recorder.speakr_publisher import SpeakrPublisher
from meeting_recorder.speakr_store import PublicationStore
from tests.speakr_fake_server import fake_speakr_server, json_body, multipart_parts


NOW = datetime(2026, 8, 20, 12, 34, 56, 789000, tzinfo=timezone.utc)
TOKEN = "speakr-token-private-sentinel"


def _meeting() -> MeetingSnapshot:
    return MeetingSnapshot(
        OccurrenceKey.single("calendar", "event"), "Design review", NOW,
        NOW + timedelta(hours=1), ("Alice", "Bob"), "Public notes", "Room 7", True,
    )


def _sidecar(
    media: Path, meeting: MeetingSnapshot | None, *, recording_filename: str | None = None,
) -> MeetingSidecar:
    return MeetingSidecar(
        recording_filename or media.name, "fallback.mkv", NOW, NOW + timedelta(minutes=1), meeting,
    )


def _output(callable_object, *args, **kwargs) -> tuple[int, str]:
    output = io.StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        result = callable_object(*args, **kwargs)
    return result, output.getvalue()


def test_speakr_parser_has_only_explicit_upload_path_and_no_credential_options() -> None:
    parser = build_parser()
    args = parser.parse_args(["speakr", "upload", "recording.mkv"])
    assert args.command == "speakr"
    assert args.speakr_command == "upload"
    assert args.path == "recording.mkv"
    for option in ("--token", "--url", "--secret"):
        try:
            parser.parse_args(["speakr", "upload", option, "value"])
        except SystemExit:
            pass
        else:
            raise AssertionError(f"forbidden Speakr option accepted: {option}")


def test_speakr_url_is_typed_only_and_token_is_environment_only() -> None:
    config = cast(Config, SimpleNamespace(speakr_url="https://configured.example/"))
    with patch.dict(os.environ, {
        "MEETING_RECORDER_SPEAKR_URL": "HTTP://env.example:80/",
        "MEETING_RECORDER_SPEAKR_TOKEN": TOKEN,
    }, clear=False):
        assert resolve_speakr_url(config) == "http://env.example"
        assert require_speakr_token() == TOKEN
    assert resolve_speakr_url(config, {}) == "https://configured.example"

    for value in ("", "https://example.test/path", "https://example.test?token=x"):
        try:
            resolve_speakr_url(config, {"MEETING_RECORDER_SPEAKR_URL": value})
        except ValueError:
            pass
        else:
            raise AssertionError("malformed Speakr URL was accepted")
    for value in ("", "x y", "x\r\ny", "x\x00", "x" * 4097):
        try:
            require_speakr_token({"MEETING_RECORDER_SPEAKR_TOKEN": value})
        except ValueError:
            pass
        else:
            raise AssertionError("malformed Speakr token was accepted")


def test_speakr_command_config_errors_are_safe_and_do_not_construct_publisher() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MEETING_RECORDER_SPEAKR_URL", None)
        os.environ.pop("MEETING_RECORDER_SPEAKR_TOKEN", None)
        result, output = _output(_cmd_speakr_upload, SimpleNamespace(speakr_url=""), "missing.mkv")
    assert result == 2
    assert "invalid instance URL" in output
    assert TOKEN not in output

    with patch.dict(os.environ, {"MEETING_RECORDER_SPEAKR_TOKEN": TOKEN}, clear=False), \
            patch("meeting_recorder.__main__.resolve_speakr_url", side_effect=ValueError):
        result, output = _output(
            _cmd_speakr_upload, SimpleNamespace(speakr_url="https://example.test"), "missing.mkv",
        )
    assert result == 2 and TOKEN not in output

    with patch.dict(os.environ, {"MEETING_RECORDER_SPEAKR_TOKEN": "bad token"}, clear=False), \
            patch("meeting_recorder.__main__.resolve_speakr_url", return_value="https://example.test"):
        result, output = _output(
            _cmd_speakr_upload, SimpleNamespace(speakr_url="https://example.test"), "missing.mkv",
        )
    assert result == 2 and TOKEN not in output


def test_speakr_command_state_messages_are_safe_and_stable() -> None:
    states = (
        (PublicationState.PUBLISHED, 0, "published"),
        (PublicationState.PUBLISHED, 0, "already published"),
        (PublicationState.TRANSFER_REJECTED, 1, "HTTP 503"),
        (PublicationState.TRANSFER_UNKNOWN, 1, "media was not re-sent"),
        (PublicationState.METADATA_PENDING, 1, "no media re-upload"),
    )
    for state, expected_code, expected_text in states:
        result = SimpleNamespace(
            job=SimpleNamespace(state=state, last_http_status=503),
            already_published=expected_text == "already published",
        )
        with patch.dict(os.environ, {"MEETING_RECORDER_SPEAKR_TOKEN": TOKEN}, clear=False), \
                patch("meeting_recorder.__main__.resolve_speakr_url", return_value="https://example.test"), \
                patch("meeting_recorder.__main__.require_speakr_token", return_value=TOKEN), \
                patch("meeting_recorder.speakr_store.PublicationStore"), \
                patch("meeting_recorder.speakr_http.StdlibSpeakrTransport"), \
                patch("meeting_recorder.speakr_publisher.SpeakrPublisher") as publisher:
            publisher.return_value.publish.return_value = result
            exit_code, output = _output(
                _cmd_speakr_upload, SimpleNamespace(speakr_url="https://example.test"), "recording.mkv",
            )
        assert exit_code == expected_code
        assert expected_text in output
        assert TOKEN not in output


def test_local_server_acceptance_is_restart_safe_and_sends_current_public_metadata() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        media = root / "fallback.mkv"
        payload = b"exact recording bytes\x00\xff"
        media.write_bytes(payload)
        os.utime(media, ns=(4_000_000_000, 4_000_000_000))
        write_sidecar(sidecar_path(media), _sidecar(media, _meeting()))
        store = PublicationStore(root / "state" / "publications.sqlite3", clock=lambda: 1_000)

        with fake_speakr_server(patch_statuses=(503, 204)) as (url, server):
            publisher = SpeakrPublisher(
                store, StdlibSpeakrTransport(timeout_seconds=2, chunk_size=3), chunk_size=3,
            )
            first = publisher.publish(media, url, TOKEN)
            second = publisher.publish(media, url, TOKEN)
            third = publisher.publish(media, url, TOKEN)

        assert first.job.state is PublicationState.METADATA_PENDING
        assert second.job.state is PublicationState.PUBLISHED
        assert third.already_published
        assert [request.method for request in server.requests] == ["POST", "PATCH", "PATCH"]

        upload, first_patch, second_patch = server.requests
        assert upload.path == "/api/v1/recordings/upload"
        assert upload.headers["authorization"] == "Bearer " + TOKEN
        parts = multipart_parts(upload)
        assert parts["file"][1] == payload
        assert parts["file_last_modified"][1] == b"4000"
        assert second_patch.path == "/api/v1/recordings/42"
        assert second_patch.headers["authorization"] == "Bearer " + TOKEN
        assert json_body(second_patch) == {
            "title": "Design review",
            "meeting_date": "2026-08-20T12:34:56.789000Z",
            "notes": "Public notes\n\nLocation: Room 7",
            "participants": "Alice, Bob",
        }
        assert hashlib.sha256(payload).hexdigest().encode() in store.database_path.read_bytes()
        assert TOKEN.encode() not in store.database_path.read_bytes()


def test_unmatched_sidecar_uses_current_filename_and_mtime_without_private_metadata() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        media = root / "renamed.mkv"
        media.write_bytes(b"unmatched")
        os.utime(media, ns=(5_000_000_000, 5_000_000_000))
        write_sidecar(
            sidecar_path(media), _sidecar(media, _meeting(), recording_filename="other.mkv"),
        )
        store = PublicationStore(root / "state" / "publications.sqlite3", clock=lambda: 1_000)
        with fake_speakr_server() as (url, server):
            result = SpeakrPublisher(
                store, StdlibSpeakrTransport(timeout_seconds=2),
            ).publish(media, url, TOKEN)
        assert result.job.state is PublicationState.PUBLISHED
        assert json_body(server.requests[1]) == {
            "title": "renamed",
            "meeting_date": "1970-01-01T00:00:05Z",
            "notes": "",
            "participants": "",
        }
