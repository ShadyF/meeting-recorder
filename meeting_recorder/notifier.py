"""Desktop notifications with action buttons via libnotify (gi.repository.Notify).

Action callbacks are delivered on the GLib main loop that the daemon already runs.
Falls back to `notify-send` (no buttons) if the Notify typelib is unavailable.
"""

from __future__ import annotations

import subprocess
from typing import Callable

from .utils import LOG

_APP_NAME = "Smart Meeting Recorder"

try:
    import gi
    gi.require_version("Notify", "0.7")
    from gi.repository import Notify  # type: ignore
    _HAVE_NOTIFY = True
except (ImportError, ValueError):  # pragma: no cover - depends on system typelibs
    _HAVE_NOTIFY = False

# How long transient info notifications stay before auto-closing (ms).
_INFO_TIMEOUT_MS = 2000


class Notifier:
    def __init__(self) -> None:
        self._ready = False
        if _HAVE_NOTIFY:
            try:
                Notify.init(_APP_NAME)
                self._ready = True
            except Exception:  # pragma: no cover
                LOG.exception("Notify.init failed; using notify-send fallback")
        # Keep a reference so the notification isn't GC'd before the user acts.
        self._active = None
        self._live: set = set()  # notifications still awaiting a click/close

    # -- fallback -----------------------------------------------------------
    @staticmethod
    def _fallback(summary: str, body: str) -> None:
        try:
            # -t lets the banner expire on its own; no -e, so it stays in the
            # notification centre.
            subprocess.run(["notify-send", "-a", _APP_NAME,
                            "-t", str(_INFO_TIMEOUT_MS), summary, body],
                           timeout=5, check=False)
        except (subprocess.SubprocessError, FileNotFoundError):
            LOG.info("[notify] %s — %s", summary, body)

    # -- simple info -------------------------------------------------------
    def info(self, summary: str, body: str = "", icon: str = "media-record",
             on_click: Callable[[], None] | None = None,
             click_label: str = "Open", persistent: bool = False) -> None:
        """Show a notification that stays in the notification centre.

        `persistent=False` (default) lets the banner slide away after ~2s;
        `persistent=True` keeps it on screen until the user acts — use it for
        actionable notifications so the click target doesn't disappear.

        We deliberately never set the 'transient' hint or call close() — either
        would drop the notification from the centre. `on_click` becomes a named
        action, which the shell renders as a button on the notification.
        """
        if not self._ready:
            self._fallback(summary, body)
            return
        note = Notify.Notification.new(summary, body, icon)
        if persistent:
            # CRITICAL urgency is what makes GNOME hold the banner open.
            note.set_urgency(Notify.Urgency.CRITICAL)
            note.set_timeout(Notify.EXPIRES_NEVER)
        else:
            note.set_timeout(_INFO_TIMEOUT_MS)
        if on_click is not None:
            # A named (non-"default") action renders as a button. "default" would
            # instead make the whole banner clickable with no visible button.
            note.add_action("open-folder", click_label,
                            lambda _n, _a: self._invoke(on_click))
        # Hold a reference until it closes, otherwise the action callback dies.
        self._live.add(note)
        note.connect("closed", lambda n: self._live.discard(n))
        try:
            note.show()
        except Exception:  # pragma: no cover
            self._live.discard(note)
            self._fallback(summary, body)

    @property
    def has_live_notifications(self) -> bool:
        """True while a notification we own is still on screen.

        Its buttons are callbacks into *this* process, so a short-lived
        command has to outlive the notification or clicking does nothing.
        """
        return bool(self._live)

    @staticmethod
    def _invoke(cb: Callable[[], None]) -> None:
        try:
            cb()
        except Exception:  # pragma: no cover - never let a click kill the daemon
            LOG.exception("notification click handler failed")

    # -- meeting capture prompt --------------------------------------------
    def prompt_capture_mode(self, app_name: str, timeout_seconds: int,
                            on_video: Callable[[], None],
                            on_audio_only: Callable[[], None],
                            on_ignore: Callable[[], None]) -> None:
        """Ask for a capture mode, treating every dismissal as Ignore."""
        summary = "Meeting detected"
        body = f"A {app_name} call is in progress. Choose what to capture."

        # A passive notification cannot collect consent, so never start a run.
        if not self._ready or not self._supports_actions():
            self._fallback(summary, body)
            self._invoke(on_ignore)
            return

        note = Notify.Notification.new(summary, body, "camera-video")
        note.set_urgency(Notify.Urgency.CRITICAL)
        note.set_timeout(timeout_seconds * 1000)
        handled = False

        # Actions can be followed by a close signal; consume only the first event.
        def _dispatch(callback: Callable[[], None]) -> None:
            nonlocal handled
            if handled:
                return
            handled = True
            if self._active is note:
                self._active = None
            self._invoke(callback)

        note.add_action("video", "Video", lambda _n, _a: _dispatch(on_video))
        note.add_action("audio-only", "Audio only",
                        lambda _n, _a: _dispatch(on_audio_only))
        note.add_action("ignore", "Ignore", lambda _n, _a: _dispatch(on_ignore))
        note.connect("closed", lambda _n: _dispatch(on_ignore))
        self._active = note
        try:
            note.show()
        except Exception:  # pragma: no cover
            self._active = None
            self._fallback(summary, body)
            _dispatch(on_ignore)

    @staticmethod
    def _supports_actions() -> bool:
        """Return whether the active notification server exposes action buttons."""
        try:
            return "actions" in Notify.get_server_caps()
        except Exception:  # pragma: no cover - server capability query failed
            return False

    def close_active(self) -> None:
        if self._active is not None:
            try:
                self._active.close()
            except Exception:  # pragma: no cover
                pass
            self._active = None
