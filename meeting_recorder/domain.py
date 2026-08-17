"""Stable domain concepts shared across recording workflows."""

from __future__ import annotations

from enum import Enum


class CaptureMode(str, Enum):
    """The media composition requested for one recording run."""

    AUDIO_ONLY = "audio-only"
    AUDIO_VIDEO = "audio-video"


class VideoSource(str, Enum):
    """The configured screen content to include in an audio-video recording."""

    FULLSCREEN = "fullscreen"
    WINDOW = "window"
    AREA = "area"

    @classmethod
    def parse(cls, value: object) -> "VideoSource":
        """Parse an external config value, defaulting unknown values to fullscreen."""
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value))
        except ValueError:
            return cls.FULLSCREEN
