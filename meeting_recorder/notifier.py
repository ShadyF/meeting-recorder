"""Desktop notifications with action buttons via libnotify (gi.repository.Notify).

Action callbacks are delivered on the GLib main loop that the daemon already runs.
Falls back to `notify-send` (no buttons) if the Notify typelib is unavailable.
"""

from __future__ import annotations

import subprocess
from typing import Any, Callable

from .utils import LOG

_APP_NAME = "Smart Meeting Recorder"
GLib: Any = None
Notify: Any = None

try:
    import gi
    gi.require_version("Notify", "0.7")
    from gi.repository import GLib as _GLib, Notify as _Notify  # type: ignore
    GLib = _GLib
    Notify = _Notify
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
        self._prompt_generation = 0
        self._prompt_timer_source = None
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

        # Invalidate and dismiss any older prompt before installing this one.
        self._prompt_generation += 1
        generation = self._prompt_generation
        self._cancel_prompt_timeout()
        previous_note, self._active = self._active, None
        if previous_note is not None:
            self._close_notification(previous_note)

        # A passive notification cannot collect consent, so never start a run.
        if not self._ready or not self._supports_actions():
            self._fallback(summary, body)
            self._invoke(on_ignore)
            return

        note = Notify.Notification.new(summary, body, "camera-video")
        note.set_urgency(Notify.Urgency.CRITICAL)
        note.set_timeout(timeout_seconds * 1000)
        handled = False
        callbacks = {
            "video": on_video,
            "audio-only": on_audio_only,
            "ignore": on_ignore,
        }

        # Validate identity before consuming one terminal event for this prompt.
        def _dispatch(action_id: str, close_note: bool = True) -> None:
            nonlocal handled
            if (handled or generation != self._prompt_generation or
                    self._active is not note):
                return
            handled = True
            self._cancel_prompt_timeout()
            self._active = None
            if close_note:
                self._close_notification(note)
            self._invoke(callbacks.get(action_id, on_ignore))

        def _on_action(_note: Any, action_id: str) -> None:
            _dispatch(action_id)

        def _on_timeout() -> bool:
            if (handled or generation != self._prompt_generation or
                    self._active is not note):
                return False
            self._prompt_timer_source = None
            _dispatch("ignore")
            return False

        note.add_action("video", "Video", _on_action)
        note.add_action("audio-only", "Audio only", _on_action)
        note.add_action("ignore", "Ignore", _on_action)
        note.add_action("default", "", _on_action)
        note.connect("closed", lambda _n: _dispatch("ignore", close_note=False))
        self._active = note
        self._prompt_timer_source = self._schedule_prompt_timeout(
            timeout_seconds, _on_timeout)
        if self._prompt_timer_source is None:
            self._fallback(summary, body)
            _dispatch("ignore")
            return

        try:
            note.show()
        except Exception:  # pragma: no cover
            self._fallback(summary, body)
            _dispatch("ignore")

    @staticmethod
    def _supports_actions() -> bool:
        """Return whether the active notification server exposes action buttons."""
        try:
            return "actions" in Notify.get_server_caps()
        except Exception:  # pragma: no cover - server capability query failed
            return False

    @staticmethod
    def _schedule_prompt_timeout(timeout_seconds: int,
                                 callback: Callable[[], bool]) -> Any | None:
        """Arm a main-loop timeout rather than trusting the server hint alone."""
        if GLib is None:
            return None
        try:
            return GLib.timeout_add(max(1, timeout_seconds * 1000), callback)
        except Exception:  # pragma: no cover - broken event-loop integration
            LOG.exception("Could not schedule capture prompt timeout")
            return None

    def _cancel_prompt_timeout(self) -> None:
        """Cancel the timer owned by the currently active prompt, if any."""
        source, self._prompt_timer_source = self._prompt_timer_source, None
        if source is None or GLib is None:
            return
        try:
            GLib.source_remove(source)
        except Exception:  # pragma: no cover - source already removed externally
            LOG.debug("Could not remove capture prompt timeout", exc_info=True)

    @staticmethod
    def _close_notification(note: Any) -> None:
        """Dismiss a native prompt without letting close failures escape."""
        try:
            note.close()
        except Exception:  # pragma: no cover - notification server disappeared
            LOG.debug("Could not close capture prompt", exc_info=True)

    def close_active(self) -> None:
        # Invalidate first so a delayed native close cannot affect a later prompt.
        self._prompt_generation += 1
        self._cancel_prompt_timeout()
        note, self._active = self._active, None
        if note is not None:
            self._close_notification(note)
