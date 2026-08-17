"""Public domain-type tests."""

from meeting_recorder.domain import CaptureMode


def test_capture_mode_has_only_the_supported_media_compositions():
    assert [mode.value for mode in CaptureMode] == ["audio-only", "audio-video"]
