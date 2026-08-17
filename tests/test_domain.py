"""Public domain-type tests."""

from meeting_recorder.domain import CaptureMode, VideoSource


def test_capture_mode_has_only_the_supported_media_compositions():
    assert [mode.value for mode in CaptureMode] == ["audio-only", "audio-video"]


def test_video_source_parses_supported_values_and_defaults_unknown_values():
    assert [source.value for source in VideoSource] == ["fullscreen", "window", "area"]
    assert VideoSource.parse("window") is VideoSource.WINDOW
    assert VideoSource.parse("unexpected") is VideoSource.FULLSCREEN
