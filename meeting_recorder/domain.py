"""Stable domain concepts shared across recording workflows."""

from __future__ import annotations

from enum import Enum
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


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


@dataclass(frozen=True)
class CompletedRecording:
    """The immutable outcome of one successfully finalized recording."""

    path: Path
    source_app: str
    requested_capture_mode: CaptureMode
    has_audio: bool
    has_video: bool
    capture_started_at: datetime
    capture_ended_at: datetime

    def __post_init__(self) -> None:
        """Reject ambiguous capture times before they enter the immutable result."""
        for timestamp in (self.capture_started_at, self.capture_ended_at):
            if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
                raise ValueError("capture timestamps must be timezone-aware UTC")
        if self.capture_ended_at < self.capture_started_at:
            raise ValueError("capture end must not precede capture start")
