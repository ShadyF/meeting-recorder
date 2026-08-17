"""Configuration loading: shipped defaults merged with the user's overrides.

User config lives at ~/.config/meeting-recorder/config.json (XDG-respecting).
Any key omitted there falls back to config/default_config.json.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .utils import LOG, expand_path

# Ships inside the package so it resolves the same from a source checkout and an
# installed system package.
_DEFAULTS_FILE = Path(__file__).resolve().parent / "default_config.json"


def _user_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return expand_path(base) / "meeting-recorder" / "config.json"


@dataclass
class AllowEntry:
    """One allowlist rule: substring `match` -> friendly `app` display name."""
    match: str
    app: str


@dataclass
class Config:
    output_dir: Path
    record_screen: bool
    video_source: str           # "fullscreen" | "window" | "area"
    capture_region: str         # "x,y,w,h" used when video_source == "area"
    show_cursor: bool           # draw the mouse pointer into the video
    wayland_restore_token: str  # portal ScreenCast token, so we prompt only once
    record_mic: bool
    record_system_audio: bool
    mic_volume: float
    system_volume: float
    normalize_voice: bool
    noise_cancellation: bool
    noise_model_path: str
    auto_record: bool
    framerate: int
    video_codec: str
    video_preset: str
    container: str
    prompt_timeout_seconds: int
    start_debounce_seconds: float
    stop_debounce_seconds: float
    poll_interval_seconds: float
    min_recording_seconds: float
    allowlist: list[AllowEntry] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        allow = [AllowEntry(match=str(e["match"]).lower(), app=str(e["app"]))
                 for e in data.get("allowlist", [])]
        return cls(
            output_dir=expand_path(data["output_dir"]),
            record_screen=bool(data["record_screen"]),
            video_source=str(data.get("video_source", "fullscreen")),
            capture_region=str(data.get("capture_region", "")),
            show_cursor=bool(data.get("show_cursor", True)),
            wayland_restore_token=str(data.get("wayland_restore_token", "")),
            record_mic=bool(data["record_mic"]),
            record_system_audio=bool(data["record_system_audio"]),
            mic_volume=float(data.get("mic_volume", 1.0)),
            system_volume=float(data.get("system_volume", 1.0)),
            normalize_voice=bool(data.get("normalize_voice", True)),
            noise_cancellation=bool(data.get("noise_cancellation", True)),
            noise_model_path=str(data.get("noise_model_path", "")),
            auto_record=bool(data["auto_record"]),
            framerate=int(data["framerate"]),
            video_codec=str(data["video_codec"]),
            video_preset=str(data["video_preset"]),
            container=str(data["container"]),
            prompt_timeout_seconds=int(data["prompt_timeout_seconds"]),
            start_debounce_seconds=float(data["start_debounce_seconds"]),
            stop_debounce_seconds=float(data["stop_debounce_seconds"]),
            poll_interval_seconds=float(data["poll_interval_seconds"]),
            min_recording_seconds=float(data["min_recording_seconds"]),
            allowlist=allow,
        )


def _load_defaults() -> dict[str, Any]:
    with _DEFAULTS_FILE.open(encoding="utf-8") as fh:
        return json.load(fh)


def load_defaults() -> dict[str, Any]:
    """The shipped defaults, with no user overrides — used by Reset."""
    return _load_defaults()


def _normalize_user_overrides(user: dict[str, Any]) -> dict[str, Any]:
    """Map the retired capture-mode key to the canonical video-source key."""
    normalized = dict(user)
    legacy_source = normalized.pop("capture_mode", None)
    if "video_source" not in normalized and legacy_source is not None:
        normalized["video_source"] = legacy_source
    return normalized


def _load_effective_config() -> dict[str, Any]:
    """Merge normalized user overrides into the shipped defaults once."""
    data = _load_defaults()
    user_path = _user_config_path()
    if user_path.is_file():
        try:
            with user_path.open(encoding="utf-8") as fh:
                user = _normalize_user_overrides(json.load(fh))
            data.update(user)  # shallow merge is enough for this flat schema
            LOG.info("Loaded user config from %s", user_path)
        except (json.JSONDecodeError, OSError) as exc:
            LOG.warning("Ignoring bad user config %s: %s", user_path, exc)
    return data


def load_config() -> Config:
    """Load defaults, deep-merge the user's config.json on top, return a Config."""
    return Config.from_dict(_load_effective_config())


def write_default_user_config() -> Path:
    """Write the shipped defaults to the user config path (for `config` subcommand)."""
    dest = _user_config_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        dest.write_text(json.dumps(_load_defaults(), indent=2) + "\n", encoding="utf-8")
    return dest


def user_config_path() -> Path:
    """Public accessor for the user config file location."""
    return _user_config_path()


def load_raw_config() -> dict[str, Any]:
    """Return the effective config as a plain dict (defaults + user overrides).

    Unlike load_config(), this keeps the raw JSON shape so the settings GUI can
    edit a subset of keys and write everything (incl. the allowlist) back intact.
    """
    return _load_effective_config()


def save_user_config(data: dict[str, Any]) -> Path:
    """Write the given config dict to the user config path (pretty JSON)."""
    dest = _user_config_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    canonical_data = _normalize_user_overrides(data)
    dest.write_text(json.dumps(canonical_data, indent=2) + "\n", encoding="utf-8")
    return dest


def save_restore_token(token: str) -> None:
    """Persist the Wayland ScreenCast restore token, if it changed.

    Stored in the user config so the portal picker only appears the first time.
    Best-effort: failing to save just means the user gets asked again.
    """
    try:
        data = load_raw_config()
        if data.get("wayland_restore_token") == token:
            return
        data["wayland_restore_token"] = token
        save_user_config(data)
        LOG.info("Saved screen-capture permission for future recordings")
    except OSError as exc:
        LOG.warning("Could not save ScreenCast restore token: %s", exc)
