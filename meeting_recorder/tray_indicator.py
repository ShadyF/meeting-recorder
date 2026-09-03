"""System-tray recording control (AppIndicator3).

Shows an icon in the GNOME top bar while recording; clicking it opens a menu
with the live timer, Pause/Resume and Stop & Save. Deliberately nothing else —
the menu is for controlling the recording in front of you, not a launcher.
Requires the `ubuntu-appindicators` shell extension (enabled by default on
Ubuntu GNOME).

Preferred over the floating pill widget; the caller falls back to
recording_widget.RecordingWidget if this can't be created.
"""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AppIndicator3", "0.1")
from gi.repository import AppIndicator3, Gtk  # noqa: E402

from .recording_widget import _fmt  # noqa: E402  (shared MM:SS formatter)
from .utils import LOG  # noqa: E402

_ID = "meeting-recorder"
# Symbolic names, and our own icon as the last resort. The panel is drawn by
# gnome-shell, which resolves icons against the *shell's* theme (Yaru here) —
# not GTK's search path. Yaru ships only `media-record-symbolic`, so the plain
# `media-record` name resolved for GTK (via the legacy Humanity theme) while
# the shell found nothing and drew an invisible icon. `meeting-recorder` lives
# in hicolor, which every theme inherits.
# Our own coloured icons first: gnome-shell recolours any *-symbolic icon to the
# panel foreground, so those come out plain white and a recording indicator
# stops reading as "live". The symbolic names remain as a fallback for a source
# checkout, where our icons are not installed into a theme directory yet.
_ICON_RECORDING = ("meeting-recorder-recording", "media-record-symbolic",
                   "meeting-recorder")
_ICON_PAUSED = ("meeting-recorder-paused", "media-playback-pause-symbolic",
                "meeting-recorder")


def _icon_name(candidates: tuple[str, ...]) -> str:
    """First candidate the icon theme actually has, else our bundled icon."""
    try:
        theme = Gtk.IconTheme.get_default()
        for name in candidates:
            if theme.has_icon(name):
                return name
    except Exception:  # pragma: no cover - no theme (headless)
        LOG.debug("icon theme lookup failed", exc_info=True)
    return candidates[-1]


class RecordingTray:
    """Tray icon + menu mirroring the RecordingWidget interface."""

    def __init__(self, on_pause: Callable[[], None],
                 on_resume: Callable[[], None],
                 on_stop: Callable[[], None],
                 on_tags: Callable[[], None] | None = None) -> None:
        # Store callbacks and state for this recording session.
        self.on_pause = on_pause
        self.on_resume = on_resume
        self.on_stop = on_stop
        self.on_tags = on_tags
        self.paused = False
        self._tag_action_label: str | None = None

        # Create the panel indicator in a passive state until recording starts.
        self.ind = AppIndicator3.Indicator.new(
            _ID, _icon_name(_ICON_RECORDING),
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS)
        self.ind.set_status(AppIndicator3.IndicatorStatus.PASSIVE)

        self.menu = Gtk.Menu()

        # Live status line, e.g. "● Recording — 01:23"
        self.header = Gtk.MenuItem(label="● Recording — 00:00")
        self.header.set_sensitive(False)
        self._add(self.header)
        self._add(Gtk.SeparatorMenuItem())

        # Reserve a separate optional section without leaving a blank separator.
        self.tag_item = Gtk.MenuItem(label="")
        self.tag_item.set_no_show_all(True)
        self.tag_item.set_sensitive(False)
        self.tag_item.connect("activate", self._on_tags_activate)
        tag_accessible = self.tag_item.get_accessible()
        tag_accessible.set_name("Recording tags")
        tag_accessible.set_description(
            "Choose or retry Speakr tags without affecting recording controls.")
        self._add(self.tag_item)
        self.tag_item.hide()
        self.tag_separator = Gtk.SeparatorMenuItem()
        self.tag_separator.set_no_show_all(True)
        self._add(self.tag_separator)
        self.tag_separator.hide()

        self.pause_item = Gtk.MenuItem(label="Pause")
        self.pause_item.connect("activate", self._on_pause_activate)
        self._add(self.pause_item)

        self.stop_item = Gtk.MenuItem(label="Stop & Save")
        self.stop_item.connect("activate", lambda _i: self.on_stop())
        self._add(self.stop_item)

        self.menu.show_all()
        self.ind.set_menu(self.menu)

    def _add(self, item: Gtk.MenuItem) -> None:
        self.menu.append(item)
        item.show()

    # -- lifecycle (same surface as RecordingWidget) -----------------------
    def show(self) -> None:
        # Reset pause state before exposing the active indicator.
        self.paused = False
        self._refresh()
        self.ind.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

    def close(self) -> None:
        # Reused indicators must not expose an action from the prior recording.
        self.set_tag_action(None)

        # Remove this recording's indicator from the panel.
        self.ind.set_status(AppIndicator3.IndicatorStatus.PASSIVE)

    def update_time(self, seconds: float) -> None:
        # Update both the menu status and optional shell label.
        state = "Paused" if self.paused else "● Recording"
        self.header.set_label(f"{state} — {_fmt(seconds)}")
        # Some shells render a label next to the icon; harmless if ignored.
        self.ind.set_label(f" {_fmt(seconds)}", "00:00")

    def set_tag_action(self, label: str | None) -> None:
        """Show, update, or hide the optional tag menu section."""
        # Store the state so a stale synthetic activation remains harmless.
        self._tag_action_label = label
        if label is None:
            self.tag_item.set_sensitive(False)
            self.tag_item.hide()
            self.tag_separator.hide()
            self.menu.queue_resize()
            return

        # Explicit visibility changes update an AppIndicator menu already in use.
        self.tag_item.set_label(label)
        self.tag_item.get_accessible().set_name(label)
        self.tag_item.set_sensitive(self.on_tags is not None)
        self.tag_item.show()
        self.tag_separator.show()
        self.menu.queue_resize()

    # -- menu actions ------------------------------------------------------
    def _on_pause_activate(self, _item: Gtk.MenuItem) -> None:
        # Toggle recording state through the matching callback.
        if self.paused:
            self.paused = False
            self.on_resume()
        else:
            self.paused = True
            self.on_pause()
        self._refresh()

    def _on_tags_activate(self, _item: Gtk.MenuItem) -> None:
        # Ignore hidden, stale, or callback-free activation.
        if self._tag_action_label is None or self.on_tags is None:
            return

        # Isolate optional tag UI failures from the recording menu lifecycle.
        try:
            self.on_tags()
        except Exception:
            LOG.exception("Tag action failed")

    def _refresh(self) -> None:
        # Keep the action label and panel icon aligned with pause state.
        self.pause_item.set_label("Resume" if self.paused else "Pause")
        self.ind.set_icon_full(
            _icon_name(_ICON_PAUSED if self.paused else _ICON_RECORDING),
            "Paused" if self.paused else "Recording")
