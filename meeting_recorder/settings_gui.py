"""GTK settings window for Smart Meeting Recorder.

Edits the same ~/.config/meeting-recorder/config.json the daemon reads. Only a
curated subset of keys is exposed; everything else (e.g. the allowlist) is loaded
and written back untouched. Launch with:  python -m meeting_recorder settings
"""

from __future__ import annotations

import shutil
import subprocess
import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from .config import load_defaults, load_raw_config, save_user_config
from .utils import LOG, expand_path

CONTAINERS = ["mkv", "mp4"]
# (config value, display label)
VIDEO_SOURCES = [("fullscreen", "Full screen"),
                 ("window", "Current window"),
                 ("area", "Selected area")]


class SettingsWindow(Gtk.Window):
    def __init__(self) -> None:
        super().__init__(title="Meeting Recorder — Settings")
        self.set_default_size(600, 660)
        self.set_border_width(16)
        self.data = load_raw_config()

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add(outer)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        # Give the scrollbar its own gutter so it never overlaps the controls.
        scroller.set_overlay_scrolling(False)
        outer.pack_start(scroller, True, True, 0)

        grid = Gtk.Grid(row_spacing=10, column_spacing=14)
        grid.set_column_homogeneous(False)
        grid.set_margin_end(12)   # keep right-aligned widgets clear of the scrollbar
        grid.set_margin_start(2)
        scroller.add(grid)
        self._row = 0

        # Deliberately minimal. Everything else the daemon understands stays
        # editable in ~/.config/meeting-recorder/config.json — hiding a knob
        # here does not remove the feature, it just stops the window from
        # presenting a decision most people should not have to make.
        # Not shown, and why:
        #   framerate      30 is right for a meeting; 60 only doubles the size
        #   normalize_voice  equalising the two voices should just be on
        #   prompt_timeout_seconds  30s needs no tuning

        # --- Storage ------------------------------------------------------
        self._section(grid, "Storage")
        self.output_chooser = Gtk.FileChooserButton(
            title="Recording folder", action=Gtk.FileChooserAction.SELECT_FOLDER)
        self._field(grid, "Save folder", self.output_chooser)

        # mkv survives an interrupted recording (a killed ffmpeg still leaves a
        # playable file); mp4 needs its index written on a clean exit. Offered
        # anyway because mp4 is what most other software will accept.
        self.format_combo = Gtk.ComboBoxText()
        for c in CONTAINERS:
            self.format_combo.append_text(c)
        self._field(grid, "File format", self.format_combo)

        # --- What to record -------------------------------------------------
        self._section(grid, "What to record")
        self.screen_switch = self._switch(True)
        self._field(grid, "Screen", self.screen_switch)

        self.video_source_combo = Gtk.ComboBoxText()
        for _val, label in VIDEO_SOURCES:
            self.video_source_combo.append_text(label)
        self.video_source_combo.connect("changed", self._on_video_source_changed)
        self._field(grid, "Video source", self.video_source_combo)

        # Region row, only sensitive for "Selected area". Drag-select uses our
        # own GTK overlay (see region_select.py) rather than `slop`, so it works
        # on Wayland as well as X11.
        region_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.region_entry = Gtk.Entry()
        self.region_entry.set_placeholder_text("x,y,w,h")
        self.region_entry.set_hexpand(True)
        self.select_btn = Gtk.Button(label="Select…")
        self.select_btn.connect("clicked", self._on_select_area)
        region_box.pack_start(self.region_entry, True, True, 0)
        region_box.pack_start(self.select_btn, False, False, 0)
        self._field(grid, "Region", region_box)

        self.cursor_switch = self._switch(True)
        self._field(grid, "Show mouse cursor", self.cursor_switch)

        self.mic_switch = self._switch(True)
        self._field(grid, "Microphone", self.mic_switch)

        self.sys_switch = self._switch(True)
        self._field(grid, "System audio (the other people)", self.sys_switch)

        self.noise_switch = self._switch(True)
        self._field(grid, "Noise cancellation", self.noise_switch)

        # Applied after loudness normalisation, so these trim the balance
        # rather than set it: 1.0 leaves a source at the normalised level.
        self.mic_vol = self._volume_scale(1.0)
        self._field(grid, "Microphone volume", self.mic_vol)

        self.sys_vol = self._volume_scale(1.0)
        self._field(grid, "System audio volume", self.sys_vol)

        # --- Behavior -----------------------------------------------------
        self._section(grid, "Behavior")
        self.auto_switch = self._switch(False)
        self._field(grid, "Auto-record (skip the popup)", self.auto_switch)

        # How long the mic must stay silent before the call counts as over.
        # Apps release the mic while you are muted, which looks identical to
        # leaving the call, so this is also how long a mute can last before the
        # recording stops. The wait is trimmed off the saved file, so raising it
        # costs nothing but a lingering recording after the call. Steps by 1s;
        # the range goes to 5 minutes for anyone who mutes for long stretches.
        self.stop_spin = Gtk.SpinButton.new_with_range(0.5, 300, 1)
        self.stop_spin.set_digits(1)
        self._field(grid, "Keep recording after the call ends (s)", self.stop_spin)

        self._load_into_widgets()

        # --- Buttons ------------------------------------------------------
        self.status = Gtk.Label(label="", xalign=0)
        outer.pack_start(self.status, False, False, 0)

        # A single action row: actions on the left, dialog buttons on the
        # right, all sharing one baseline. Two stacked rows left them visually
        # unaligned.
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self.record_btn = Gtk.Button(label="● Record now")
        self.record_btn.set_tooltip_text(
            "Start recording immediately, without waiting for a meeting to be "
            "detected. Stop it from the tray icon.")
        self.record_btn.connect("clicked", self._on_record_now)
        actions.pack_start(self.record_btn, False, False, 0)

        reset = Gtk.Button(label="Reset to defaults")
        reset.connect("clicked", self._on_reset)
        actions.pack_start(reset, False, False, 0)

        # pack_end fills right-to-left, so Save ends up furthest right.
        # One button, not "Save" and "Save & Apply": saving without applying
        # leaves the daemon running the old values with no sign that anything
        # is stale, which is a trap rather than a choice worth offering.
        save = Gtk.Button(label="Save")
        save.get_style_context().add_class("suggested-action")
        save.connect("clicked", lambda _b: self._save())
        actions.pack_end(save, False, False, 0)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.close())
        actions.pack_end(cancel, False, False, 0)

        outer.pack_start(actions, False, False, 0)

    # -- load ---------------------------------------------------------------
    def _load_into_widgets(self) -> None:
        """Push self.data into every field. Used at startup and by Reset."""
        d = self.data
        self.output_chooser.set_filename(str(expand_path(d.get("output_dir", "~"))))
        container = d.get("container", "mkv")
        self.format_combo.set_active(
            CONTAINERS.index(container) if container in CONTAINERS else 0)
        self.screen_switch.set_active(bool(d.get("record_screen", True)))
        source = d.get("video_source", "fullscreen")
        self.video_source_combo.set_active(
            next((i for i, (v, _l) in enumerate(VIDEO_SOURCES) if v == source), 0))
        self.region_entry.set_text(d.get("capture_region", ""))
        self.cursor_switch.set_active(bool(d.get("show_cursor", True)))
        self.mic_switch.set_active(bool(d.get("record_mic", True)))
        self.sys_switch.set_active(bool(d.get("record_system_audio", True)))
        self.noise_switch.set_active(bool(d.get("noise_cancellation", True)))
        self.mic_vol.set_value(float(d.get("mic_volume", 1.0)))
        self.sys_vol.set_value(float(d.get("system_volume", 1.0)))
        self.auto_switch.set_active(bool(d.get("auto_record", False)))
        self.stop_spin.set_value(float(d.get("stop_debounce_seconds", 3.0)))
        self._on_video_source_changed(self.video_source_combo)  # region sensitivity

    # -- widget helpers ----------------------------------------------------
    def _section(self, grid: Gtk.Grid, title: str) -> None:
        lbl = Gtk.Label(xalign=0)
        lbl.set_markup(f"<b>{title}</b>")
        lbl.set_margin_top(8)
        grid.attach(lbl, 0, self._row, 2, 1)
        self._row += 1

    def _field(self, grid: Gtk.Grid, label: str, widget: Gtk.Widget) -> None:
        lbl = Gtk.Label(label=label, xalign=0)
        lbl.set_hexpand(True)
        grid.attach(lbl, 0, self._row, 1, 1)
        widget.set_halign(Gtk.Align.END if not isinstance(widget, Gtk.Scale)
                          else Gtk.Align.FILL)
        if isinstance(widget, (Gtk.Scale, Gtk.FileChooserButton)):
            widget.set_hexpand(True)
            widget.set_size_request(260, -1)
        grid.attach(widget, 1, self._row, 1, 1)
        self._row += 1

    @staticmethod
    def _switch(active: bool) -> Gtk.Switch:
        sw = Gtk.Switch()
        sw.set_active(bool(active))
        sw.set_halign(Gtk.Align.END)
        return sw

    # -- capture-area handlers ---------------------------------------------
    def _on_video_source_changed(self, combo: Gtk.ComboBoxText) -> None:
        is_area = VIDEO_SOURCES[max(0, combo.get_active())][0] == "area"
        self.region_entry.set_sensitive(is_area)
        self.select_btn.set_sensitive(is_area)
        self.select_btn.set_tooltip_text(
            "Drag a rectangle on the screen to set the capture region")

    def _on_select_area(self, _btn: Gtk.Button) -> None:
        """Drag-select a region on screen and fill in the entry."""
        from .region_select import select_region

        # Get out of the way: the overlay covers the screen, and the settings
        # window would otherwise be part of what the user is selecting over.
        self.hide()
        try:
            region = select_region()
        finally:
            self.show()
            self.present()
        if region:
            self.region_entry.set_text(region)
            self.status.set_markup(
                f"<span foreground='#2e7d32'>Region set to {region}.</span>")

    @staticmethod
    def _volume_scale(value: float) -> Gtk.Scale:
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 3.0, 0.05)
        scale.set_value(float(value))
        scale.set_digits(2)
        scale.set_value_pos(Gtk.PositionType.RIGHT)
        for mark in (0.0, 1.0, 2.0, 3.0):
            scale.add_mark(mark, Gtk.PositionType.BOTTOM, None)
        return scale

    # -- record now ---------------------------------------------------------
    def _on_record_now(self, _btn: Gtk.Button) -> None:
        """Start a manual recording in its own process.

        Spawns the `record` subcommand rather than recording in-process: that
        path already owns the tray icon, timer, pause/resume and the Wayland
        portal handshake, and this window must not start a second GLib loop.
        Detached, so the recording outlives the settings window.
        """
        exe = shutil.which("meeting-recorder")
        cmd = [exe, "record"] if exe else [sys.executable, "-m",
                                           "meeting_recorder", "record"]
        try:
            subprocess.Popen(cmd, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            LOG.warning("Could not start a manual recording: %s", exc)
            self.status.set_markup(
                f"<span foreground='#c62828'>Could not start recording: {exc}</span>")
            return
        self.status.set_markup(
            "<span foreground='#2e7d32'>Recording — stop it from the tray icon."
            "</span>")

    # -- reset -------------------------------------------------------------
    def _on_reset(self, _btn: Gtk.Button) -> None:
        """Put every field back to the shipped defaults, after confirming.

        Nothing is written until Save: the user can still close the window to
        back out. The allowlist and any keys this window does not expose are
        reset too — that is the point of "defaults" — so it is worth a prompt.
        """
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Reset all settings to their defaults?")
        dialog.format_secondary_text(
            "This also restores the meeting app allowlist and clears the saved "
            "screen-sharing permission. Nothing is written until you press Save.")
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return

        self.data = load_defaults()
        self._load_into_widgets()
        self.status.set_markup(
            "<span foreground='#b26a00'>Defaults loaded — press Save to apply."
            "</span>")

    # -- save --------------------------------------------------------------
    def _save(self) -> None:
        """Write the config and restart the daemon so it takes effect.

        The restart is not optional: the daemon reads its config once at
        startup, so a saved-but-not-restarted setting silently does nothing.
        The window stays open afterwards — saving is often followed by "Record
        now", and reopening the window for that is needless friction.
        """
        folder = self.output_chooser.get_filename()
        # self.data came from load_raw_config(), so keys this window does not
        # expose (allowlist, framerate, video_source, ...) are written back
        # exactly as they were.
        self.data.update({
            "output_dir": folder or self.data.get("output_dir", "~/Videos/MeetingRecorder"),
            "container": CONTAINERS[max(0, self.format_combo.get_active())],
            "record_screen": self.screen_switch.get_active(),
            "video_source": VIDEO_SOURCES[
                max(0, self.video_source_combo.get_active())][0],
            "capture_region": self.region_entry.get_text().strip(),
            "show_cursor": self.cursor_switch.get_active(),
            "record_mic": self.mic_switch.get_active(),
            "record_system_audio": self.sys_switch.get_active(),
            "noise_cancellation": self.noise_switch.get_active(),
            "mic_volume": round(self.mic_vol.get_value(), 2),
            "system_volume": round(self.sys_vol.get_value(), 2),
            "auto_record": self.auto_switch.get_active(),
            "stop_debounce_seconds": round(self.stop_spin.get_value(), 1),
        })
        path = save_user_config(self.data)
        LOG.info("Saved settings to %s", path)
        if _restart_service():
            self.status.set_markup(
                "<span foreground='#2e7d32'>Saved and applied.</span>")
            return
        # Saved, but the running daemon still has the old values — say so
        # rather than implying the change took effect.
        self.status.set_markup(
            "<span foreground='#b26a00'>Saved, but the background service could "
            "not be restarted — run <tt>meeting-recorder restart</tt> to apply."
            "</span>")


def _restart_service() -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "--user", "restart", "meeting-recorder.service"],
            capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        LOG.warning("Could not restart service: %s", exc)
        return False


def run() -> int:
    win = SettingsWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
    return 0
