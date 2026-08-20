"""Configuration loading: shipped defaults merged with the user's overrides.

User config lives at ~/.config/meeting-recorder/config.json (XDG-respecting).
Any key omitted there falls back to config/default_config.json.
"""

from __future__ import annotations

import errno
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import unicodedata

from .domain import VideoSource
from .speakr_domain import normalize_speakr_url
from .utils import LOG, expand_path

# Ships inside the package so it resolves the same from a source checkout and an
# installed system package.
_DEFAULTS_FILE = Path(__file__).resolve().parent / "default_config.json"
_FORBIDDEN_CALENDAR_KEYS = frozenset({
    "access_token",
    "authorization_code",
    "client_secret",
    "credential",
    "credential_file",
    "credential_json",
    "credentials",
    "credentials_file",
    "credentials_json",
    "google_calendar_client_secret",
    "google_calendar_credential_json",
    "google_calendar_credentials_json",
    "google_calendar_credential_file",
    "google_calendar_credentials_file",
    "google_calendar_client_credentials",
    "google_calendar_refresh_token",
    "google_calendar_access_token",
    "google_calendar_token",
    "google_calendar_authorization_code",
    "google_calendar_auth_code",
    "refresh_token",
})
_FORBIDDEN_SPEAKR_KEYS = frozenset({
    "speakr_token",
    "speakr_api_token",
    "speakr_bearer_token",
    "speakr_authorization",
    "speakr_access_token",
    "speakr_refresh_token",
    "speakr_secret",
    "speakr_password",
    "speakr_credentials",
})
_GOOGLE_CLIENT_DOCUMENT_KEYS = frozenset({
    "client_id", "client_secret", "auth_uri", "token_uri", "redirect_uris",
})


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
    video_source: VideoSource
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
    google_calendar_client_id: str | None
    google_calendar_loopback_port: Any
    speakr_url: str | None
    allowlist: list[AllowEntry] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        allow = [AllowEntry(match=str(e["match"]).lower(), app=str(e["app"]))
                 for e in data.get("allowlist", [])]
        # Preserve an explicitly empty override as invalid Calendar-only state.
        env_client = (os.environ["MEETING_RECORDER_GOOGLE_CLIENT_ID"] or None
                      if "MEETING_RECORDER_GOOGLE_CLIENT_ID" in os.environ else
                      data.get("google_calendar_client_id", ""))
        configured_speakr_url = data.get("speakr_url")
        env_speakr_url = (os.environ["MEETING_RECORDER_SPEAKR_URL"]
                          if "MEETING_RECORDER_SPEAKR_URL" in os.environ else
                          configured_speakr_url)
        return cls(
            output_dir=expand_path(data["output_dir"]),
            record_screen=bool(data["record_screen"]),
            video_source=VideoSource.parse(data.get("video_source", "fullscreen")),
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
            google_calendar_client_id=(str(env_client)
                                       if env_client is not None else None),
            # Calendar commands validate this independently so a malformed
            # optional setting never prevents normal recording startup.
            google_calendar_loopback_port=data.get("google_calendar_loopback_port", 0),
            # Speakr validates this only when its explicit command resolves it.
            speakr_url=(str(env_speakr_url) if env_speakr_url is not None else None),
            allowlist=allow,
        )


def _load_defaults() -> dict[str, Any]:
    with _DEFAULTS_FILE.open(encoding="utf-8") as fh:
        return json.load(fh)


def load_defaults() -> dict[str, Any]:
    """The shipped defaults, with no user overrides — used by Reset."""
    return _load_defaults()


def _normalize_user_overrides(user: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize settings and discard credential-shaped values."""
    normalized = dict(user)
    legacy_source = normalized.pop("capture_mode", None)
    if "video_source" not in normalized and legacy_source is not None:
        normalized["video_source"] = legacy_source
    # Credential material belongs exclusively in Secret Service, never JSON.
    for key in tuple(normalized):
        value = normalized[key]
        is_google_document = (
            key in {"installed", "web"} and isinstance(value, dict) and
            _GOOGLE_CLIENT_DOCUMENT_KEYS.issubset(value))
        is_speakr_credential = key in _FORBIDDEN_SPEAKR_KEYS or (
            key.startswith("speakr_") and any(
                term in key for term in ("token", "secret", "authorization", "credential", "password")
            )
        )
        if key in _FORBIDDEN_CALENDAR_KEYS or is_speakr_credential or is_google_document or (
                key.startswith("google_calendar_") and
                any(term in key for term in ("secret", "credential", "_token", "_code"))):
            normalized.pop(key)
    return normalized


def resolve_speakr_url(config: Config, environ: Any | None = None) -> str:
    """Resolve and validate the explicit Speakr instance URL."""
    values = os.environ if environ is None else environ
    value = values["MEETING_RECORDER_SPEAKR_URL"] if "MEETING_RECORDER_SPEAKR_URL" in values else config.speakr_url
    return normalize_speakr_url(value)


def require_speakr_token(environ: Any | None = None) -> str:
    """Require the on-demand Speakr bearer token from the process environment."""
    values = os.environ if environ is None else environ
    if "MEETING_RECORDER_SPEAKR_TOKEN" not in values:
        raise ValueError("Speakr token is not configured")
    token = values["MEETING_RECORDER_SPEAKR_TOKEN"]
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 4096
        or any(char.isspace() or unicodedata.category(char).startswith("C") for char in token)
    ):
        raise ValueError("Speakr token is invalid")
    return token


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
    """Atomically write the given config dict without exposing a partial JSON file."""
    dest = _user_config_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    canonical_data = _normalize_user_overrides(data)
    descriptor, temporary = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=dest.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as fh:
            descriptor = -1
            fh.write(json.dumps(canonical_data, indent=2) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, dest)
        _fsync_directory(dest.parent)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return dest


def _fsync_directory(directory: Path) -> None:
    """Best-effort durability for a completed same-directory config rename."""
    try:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        if exc.errno in {errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}:
            return
        raise


def validate_google_calendar_ids(value: object) -> tuple[str, ...]:
    """Validate opaque Calendar IDs without interpreting their contents."""
    if not isinstance(value, list) or len(value) > 50:
        raise ValueError("google_calendar_ids must be a list of at most 50 IDs")
    result: list[str] = []
    seen: set[str] = set()
    for calendar_id in value:
        if not isinstance(calendar_id, str) or not calendar_id or len(calendar_id) > 1024:
            raise ValueError("google_calendar_ids contains an invalid ID")
        if calendar_id not in seen:
            seen.add(calendar_id)
            result.append(calendar_id)
    return tuple(result)


def save_google_calendar_ids(calendar_ids: object) -> Path:
    """Persist only the validated Calendar selection while preserving user settings."""
    selected = list(validate_google_calendar_ids(calendar_ids))
    data = load_raw_config()
    data["google_calendar_ids"] = selected
    return save_user_config(data)


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
