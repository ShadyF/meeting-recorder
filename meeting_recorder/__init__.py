"""Smart Meeting Recorder for Linux.

A lightweight background daemon that detects when a known communication app
starts capturing the microphone (a meeting/call), asks permission via a desktop
notification, and records the screen + audio with ffmpeg until the call ends.
"""

__version__ = "0.4.4"
