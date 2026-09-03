"""Floating recording-control widget (top-right, always on top).

A small pill showing a blinking record dot + elapsed time, with Pause/Resume and
Stop buttons — like the controls other screen recorders overlay while recording.
It runs on the same GLib main loop as the daemon. Creation is guarded by the
caller so a missing display never breaks recording.
"""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from .utils import LOG

_CSS = b"""
.rec-pill { background-color: rgba(28,28,30,0.94); border-radius: 14px;
            padding: 6px 10px; }
.rec-dot { color: #ff3b30; font-size: 15px; }
.rec-time { color: #ffffff; font-family: monospace; font-size: 14px;
            font-weight: bold; }
.rec-pill button { padding: 2px 8px; min-height: 0; min-width: 0;
                   border-radius: 8px; }
.tag-action { margin-left: 4px; }
"""

_MARGIN = 16


def _fmt(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class RecordingWidget:
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
        self._shown = False
        self._blink_on = True
        self._blink_source: int | None = None

        # Create a focus-safe utility window for the floating controls.
        self.win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.win.set_decorated(False)
        self.win.set_keep_above(True)
        self.win.set_skip_taskbar_hint(True)
        self.win.set_skip_pager_hint(True)
        self.win.set_resizable(False)
        self.win.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.win.set_accept_focus(True)
        self.win.set_focus_on_map(False)

        # Transparent window so the rounded pill reads cleanly.
        self.win.set_app_paintable(True)
        screen = self.win.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            self.win.set_visual(visual)

        # Apply the compact pill treatment on the current screen.
        provider = Gtk.CssProvider()
        provider.load_from_data(_CSS)
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # Build the fixed horizontal control surface.
        pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        pill.get_style_context().add_class("rec-pill")
        self.win.add(pill)

        self.dot = Gtk.Label(label="●")  # ●
        self.dot.get_style_context().add_class("rec-dot")
        pill.pack_start(self.dot, False, False, 0)

        self.time_label = Gtk.Label(label="00:00")
        self.time_label.get_style_context().add_class("rec-time")
        pill.pack_start(self.time_label, False, False, 4)

        # Keep optional tagging visually separate and hidden until discovery resolves.
        self.tag_btn = Gtk.Button(label="")
        self.tag_btn.get_style_context().add_class("tag-action")
        self.tag_btn.set_no_show_all(True)
        self.tag_btn.set_sensitive(False)
        self.tag_btn.set_tooltip_text("Choose Speakr tags for this recording")
        self.tag_btn.connect("clicked", self._on_tags_clicked)
        tag_accessible = self.tag_btn.get_accessible()
        tag_accessible.set_name("Recording tags")
        tag_accessible.set_description(
            "Choose or retry Speakr tags without affecting recording controls.")
        pill.pack_start(self.tag_btn, False, False, 0)

        self.pause_btn = Gtk.Button(label="⏸")  # ⏸
        self.pause_btn.set_tooltip_text("Pause")
        self.pause_btn.connect("clicked", self._on_pause_clicked)
        pill.pack_start(self.pause_btn, False, False, 0)

        self.stop_btn = Gtk.Button(label="⏹")   # ⏹
        self.stop_btn.set_tooltip_text("Stop & save")
        self.stop_btn.connect("clicked", lambda _b: self.on_stop())
        pill.pack_start(self.stop_btn, False, False, 0)

    # -- lifecycle ---------------------------------------------------------
    def show(self) -> None:
        # Reveal the controls without taking focus from the active application.
        self._shown = True
        self.win.show_all()

        # Position after realize so the true size is known.
        GLib.idle_add(self._position)
        self._blink_source = GLib.timeout_add(600, self._blink)

    def close(self) -> None:
        # Clear optional state before destroying this recording's controls.
        self._shown = False
        self.set_tag_action(None)

        # Remove the visual timer before destroying the window.
        if self._blink_source is not None:
            GLib.source_remove(self._blink_source)
            self._blink_source = None

        self.win.destroy()

    def _position(self) -> bool:
        from .screencast import is_wayland
        if is_wayland():
            # Wayland clients cannot place their own windows: Gtk.Window.move()
            # is a no-op and Mutter has no layer-shell protocol. The pill still
            # works, the compositor just decides where it goes.
            LOG.info("Wayland: the compositor positions the recording controls")
            return False
        try:
            display = Gdk.Display.get_default()
            monitor = (display.get_primary_monitor()
                       or display.get_monitor(0))
            geo = monitor.get_geometry()
            w, _h = self.win.get_size()
            self.win.move(geo.x + geo.width - w - _MARGIN, geo.y + _MARGIN)
        except Exception:  # pragma: no cover - best effort
            LOG.debug("widget positioning failed", exc_info=True)
        return False

    # -- state -------------------------------------------------------------
    def update_time(self, seconds: float) -> None:
        prefix = "❚❚ " if self.paused else ""  # ❚❚ when paused
        self.time_label.set_text(prefix + _fmt(seconds))

    def set_tag_action(self, label: str | None) -> None:
        """Show, update, or hide the optional recording tag action."""
        # Store the state so a stale synthetic activation remains harmless.
        self._tag_action_label = label
        if label is None:
            self.tag_btn.set_sensitive(False)
            self.tag_btn.hide()

            # Restore the top-right position after the pill shrinks.
            if self._shown:
                GLib.idle_add(self._position)
            return

        # Explicit show and hide calls keep updates working after window.show_all().
        self.tag_btn.set_label(label)
        self.tag_btn.get_accessible().set_name(label)
        self.tag_btn.set_sensitive(self.on_tags is not None)
        self.tag_btn.show()

        # Restore the top-right position after the pill grows.
        if self._shown:
            GLib.idle_add(self._position)

    def _on_tags_clicked(self, _button: Gtk.Button) -> None:
        # Ignore hidden, stale, or callback-free activation.
        if self._tag_action_label is None or self.on_tags is None:
            return

        # Isolate optional tag UI failures from Pause and Stop controls.
        try:
            self.on_tags()
        except Exception:
            LOG.exception("Tag action failed")

    def _on_pause_clicked(self, _btn: Gtk.Button) -> None:
        # Toggle recording state through the matching callback.
        if self.paused:
            self.paused = False
            self.on_resume()
        else:
            self.paused = True
            self.on_pause()
        self._refresh_pause_visual()

    def _refresh_pause_visual(self) -> None:
        # Keep the pause control and recording dot aligned with current state.
        self.pause_btn.set_label("▶" if self.paused else "⏸")  # ▶ / ⏸
        self.pause_btn.set_tooltip_text("Resume" if self.paused else "Pause")
        if self.paused:
            self.dot.set_opacity(0.4)

    def _blink(self) -> bool:
        # Hold the dot dim while paused or alternate its recording opacity.
        if self.paused:
            self.dot.set_opacity(0.4)
        else:
            self._blink_on = not self._blink_on
            self.dot.set_opacity(1.0 if self._blink_on else 0.25)
        return True
