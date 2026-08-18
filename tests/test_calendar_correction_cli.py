"""Offline Calendar correction command behavior."""

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from meeting_recorder.__main__ import _cmd_calendar_correct, build_parser
from meeting_recorder.calendar_domain import CalendarOccurrence, OccurrenceKey, meeting_snapshot
from meeting_recorder.domain import CaptureMode, CompletedRecording
from meeting_recorder.meeting_sidecar import MeetingSidecar, sidecar_path, write_sidecar
from meeting_recorder.recording_enrichment import RecordingCorrectionService


NOW = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)


def _occurrence(event_id: str, title: str | None, visible: bool = True,
                start: datetime = NOW) -> CalendarOccurrence:
    return CalendarOccurrence(
        OccurrenceKey.single("calendar", event_id), start, start + timedelta(hours=1),
        summary=title, details_visible=visible)


def _recording(root: Path) -> Path:
    path = root / "capture.mkv"
    path.write_bytes(b"recording")
    write_sidecar(sidecar_path(path), MeetingSidecar(
        path.name, path.name, NOW, NOW + timedelta(minutes=30), None))
    return path


def test_correction_parser_enforces_actions_and_rejects_clear_refresh():
    parser = build_parser()
    args = parser.parse_args(["calendar", "correct", "capture.mkv"])
    assert args.recording == "capture.mkv" and not args.clear
    args = parser.parse_args(["calendar", "correct", "capture.mkv", "--select", "selector"])
    assert args.selector == "selector"
    for arguments in (
        ["calendar", "correct", "capture.mkv", "--select", "one", "--clear"],
    ):
        try:
            parser.parse_args(arguments)
            assert False, "select and clear must be mutually exclusive"
        except SystemExit:
            pass


def test_correction_cli_lists_cache_only_json_safe_nearby_rows():
    import meeting_recorder.__main__ as main_module

    with TemporaryDirectory() as directory:
        root = Path(directory)
        recording = _recording(root)
        occurrence = _occurrence("event", "Title\nwith control")
        service_module = __import__("meeting_recorder.recording_enrichment", fromlist=["x"])
        original = service_module.cache_only_occurrence_provider
        try:
            service_module.cache_only_occurrence_provider = lambda: (lambda: (occurrence,))
            output = io.StringIO()
            with redirect_stdout(output):
                assert _cmd_calendar_correct(None, str(recording), False, None, False) == 0
        finally:
            service_module.cache_only_occurrence_provider = original
        row = json.loads(output.getvalue())
        assert row["title"] == "Title\nwith control"
        assert "description" not in row and "participants" not in row
        assert row["scheduled_utc"].endswith("Z")


def test_correction_cli_refreshes_before_read_and_falls_back_after_failure():
    import meeting_recorder.__main__ as main_module
    import meeting_recorder.recording_enrichment as enrichment_module

    with TemporaryDirectory() as directory:
        recording = _recording(Path(directory))
        calls = []
        original_refresh = main_module._correction_refresh
        original_provider = enrichment_module.cache_only_occurrence_provider
        main_module._correction_refresh = lambda _cfg: calls.append("refresh")
        enrichment_module.cache_only_occurrence_provider = lambda: (lambda: ())
        try:
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                assert _cmd_calendar_correct(None, str(recording), True, None, False) == 0
        finally:
            main_module._correction_refresh = original_refresh
            enrichment_module.cache_only_occurrence_provider = original_provider
        assert calls == ["refresh"] and output.getvalue() == ""


def test_correction_cli_selects_visible_and_hidden_and_clear_needs_no_config():
    import meeting_recorder.recording_enrichment as enrichment_module

    with TemporaryDirectory() as directory:
        root = Path(directory)
        recording = _recording(root)
        visible = _occurrence("visible", "Visible")
        hidden = _occurrence("hidden", "Private", visible=False)
        original_provider = enrichment_module.cache_only_occurrence_provider
        enrichment_module.cache_only_occurrence_provider = lambda: (lambda: (visible, hidden))
        try:
            selector = next(item.selector for item in
                            RecordingCorrectionService((visible, hidden)).list_nearby(recording)
                            if item.key == visible.key)
            output = io.StringIO()
            with redirect_stdout(output):
                assert _cmd_calendar_correct(None, str(recording), False, selector, False) == 0
            assert "Visible" in output.getvalue()
            selected = Path(output.getvalue().strip().split(": ", 1)[1])
            assert selected.exists()

            hidden_selector = RecordingCorrectionService((hidden,)).list_nearby(selected)[0].selector
            output = io.StringIO()
            with redirect_stdout(output):
                assert _cmd_calendar_correct(None, str(selected), False, hidden_selector, False) == 0
            selected = Path(output.getvalue().strip().split(": ", 1)[1])
            assert selected.name == "capture.mkv"
        finally:
            enrichment_module.cache_only_occurrence_provider = original_provider

        output = io.StringIO()
        with redirect_stdout(output):
            assert _cmd_calendar_correct(None, str(selected), False, None, True) == 0
        assert "Recording:" in output.getvalue() and not sidecar_path(selected).exists()


def test_correction_cli_rejects_missing_or_stale_selection_without_mutating_media():
    import meeting_recorder.recording_enrichment as enrichment_module

    with TemporaryDirectory() as directory:
        recording = _recording(Path(directory))
        original_provider = enrichment_module.cache_only_occurrence_provider
        enrichment_module.cache_only_occurrence_provider = lambda: (lambda: ())
        try:
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                assert _cmd_calendar_correct(None, str(recording), False, None, False) == 0
            assert recording.read_bytes() == b"recording"
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                assert _cmd_calendar_correct(None, str(recording), False, "bad-selector", True) == 0
        finally:
            enrichment_module.cache_only_occurrence_provider = original_provider
