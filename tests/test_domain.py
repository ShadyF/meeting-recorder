"""Public domain-type tests."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from meeting_recorder.calendar_domain import MeetingSnapshot, OccurrenceKey
from meeting_recorder.domain import CaptureMode, CompletedRecording, VideoSource


def test_capture_mode_has_only_the_supported_media_compositions():
    assert [mode.value for mode in CaptureMode] == ["audio-only", "audio-video"]


def test_video_source_parses_supported_values_and_defaults_unknown_values():
    assert [source.value for source in VideoSource] == ["fullscreen", "window", "area"]
    assert VideoSource.parse("window") is VideoSource.WINDOW
    assert VideoSource.parse("unexpected") is VideoSource.FULLSCREEN


def test_completed_recording_keeps_an_immutable_optional_meeting_snapshot():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    meeting = MeetingSnapshot(
        OccurrenceKey.single("calendar", "event"), "Review", start,
        start + timedelta(hours=1), (), None, None, True)
    completed = CompletedRecording(Path("capture.mkv"), "Manual", CaptureMode.AUDIO_ONLY,
                                   True, False, start, start, meeting)
    assert completed.meeting == meeting
    try:
        completed.meeting = None
        assert False, "CompletedRecording must remain immutable"
    except AttributeError:
        pass
