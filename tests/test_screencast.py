"""Pure portal source-selection tests."""

from meeting_recorder.screencast import SOURCE_MONITOR, SOURCE_WINDOW, source_types_for


def test_video_source_maps_to_the_available_portal_source_types():
    assert source_types_for("fullscreen") == SOURCE_MONITOR
    assert source_types_for("window") == SOURCE_WINDOW
    assert source_types_for("area") == SOURCE_MONITOR
