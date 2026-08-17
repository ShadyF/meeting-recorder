"""Stable domain concepts shared across recording workflows."""

from __future__ import annotations

from enum import Enum


class CaptureMode(str, Enum):
    """The media composition requested for one recording run."""

    AUDIO_ONLY = "audio-only"
    AUDIO_VIDEO = "audio-video"
