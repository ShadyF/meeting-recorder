"""Pure portal source-selection tests."""

from meeting_recorder.screencast import SOURCE_MONITOR, SOURCE_WINDOW, source_types_for
from meeting_recorder.domain import VideoSource


def test_video_source_maps_to_the_available_portal_source_types():
    assert source_types_for(VideoSource.FULLSCREEN) == SOURCE_MONITOR
    assert source_types_for(VideoSource.WINDOW) == SOURCE_WINDOW
    assert source_types_for(VideoSource.AREA) == SOURCE_MONITOR
