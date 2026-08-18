"""Ties detector -> notifier -> recorder together as a small state machine.

States: IDLE -> PROMPTING -> RECORDING -> IDLE.
- Meeting detected  : prompt the user (or auto-record if configured).
- User clicks Record: start the recorder.
- Meeting ends      : stop + save, notify, return to IDLE.
An ignored session is remembered so we don't re-prompt for the same call.

Only two notifications are shown, deliberately: the Record/Ignore prompt, and
the "saved" result at the end (plus a failure notice, which would otherwise
lose a recording silently). Progress and status are the tray icon's job —
anything more is noise during a call. "Saved" carries an Open Folder button:
a button has to be clicked, so the file manager never appears unbidden.
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Callable

from .config import Config
from .domain import CaptureMode, CompletedRecording
from .notifier import Notifier
from .recorder import FinalizationHandle, Recorder
from .utils import LOG, build_output_path, open_folder


class State(enum.Enum):
    IDLE = "idle"
    PROMPTING = "prompting"
    RECORDING = "recording"


class Controller:
    def __init__(self, cfg: Config, notifier: Notifier, recorder: Recorder):
        self.cfg = cfg
        self.notifier = notifier
        self.recorder = recorder
        self.state = State.IDLE
        self._app: str | None = None
        self._ignored_session = False
        self._widget = None          # active tray or floating recording controls
        self._tray = None            # AppIndicator reused across recordings
        self._timer_source = None    # GLib timeout id for the elapsed-time updates
        self._session = None         # Wayland ScreenCast session, while recording
        self._pending_path = None    # output path awaiting the portal handshake
        self._pending_capture_mode: CaptureMode | None = None
        self._handles: set[FinalizationHandle] = set()
        self._reserved_paths: set[Path] = set()
        self._manual = False         # started by `record`, not by the detector
        # Called with the saved Path (or None) once a recording is fully
        # finalized. `record` uses it to know when it can exit.
        self.on_finished: Callable[[CompletedRecording | None], None] | None = None

    # -- called by `record` (no detector involved) --------------------------
    def start_manual(self, app_name: str = "Manual") -> None:
        """Start recording right now, skipping detection and the prompt.

        Goes through the same path as a detected meeting so a manual recording
        gets the identical controls: tray icon (or pill), live timer, pause and
        resume, and — on Wayland — the ScreenCast handshake.
        """
        if self.state is not State.IDLE:
            LOG.warning("start_manual() ignored: already %s", self.state.value)
            return
        self._app = app_name
        self._ignored_session = False
        self._manual = True
        self._begin_recording()

    def stop_manual(self) -> bool:
        """Stop a manual recording; `on_finished` fires when the file is ready."""
        started = self._finish_recording() if self.state is State.RECORDING else False
        self.state = State.IDLE
        self._app = None
        return started

    # -- called by the detector --------------------------------------------
    def on_meeting_start(self, app_name: str) -> None:
        if self.state is not State.IDLE:
            return
        self._app = app_name
        self._ignored_session = False
        if self.cfg.auto_record:
            self._begin_recording()
            return
        self.state = State.PROMPTING
        self.notifier.prompt_record(
            app_name, self.cfg.prompt_timeout_seconds,
            on_record=self._on_user_record,
            on_ignore=self._on_user_ignore,
        )

    def on_meeting_stop(self, overshoot: float = 0.0) -> None:
        """`overshoot` = seconds recorded after the call's audio actually ended
        (the detector's debounce wait); it gets trimmed off the saved file.
        """
        self.notifier.close_active()
        if self.state is State.RECORDING:
            self._finish_recording(trim_end=overshoot)
        self.state = State.IDLE
        self._app = None

    # -- notification callbacks --------------------------------------------
    def _on_user_record(self) -> None:
        if self.state is State.PROMPTING:
            self._begin_recording()

    def _on_user_ignore(self) -> None:
        if self.state is State.PROMPTING:
            LOG.info("User declined recording for %s", self._app)
            self._ignored_session = True
            self.state = State.IDLE

    # -- recording helpers -------------------------------------------------
    def _begin_recording(self) -> None:
        path = build_output_path(self.cfg.output_dir, self._app or "Meeting",
                                 self.cfg.container)
        path = self._reserve_path(path)
        capture_mode = (CaptureMode.AUDIO_VIDEO if self.cfg.record_screen
                        else CaptureMode.AUDIO_ONLY)
        self.state = State.RECORDING
        # Show the controls straight away, before any capture starts. On Wayland
        # the portal handshake happens first and can take seconds (or stall on a
        # dialog), and leaving the screen empty in the meantime reads as "Record
        # did nothing" — the tray icon is the only feedback the user gets.
        self._show_widget()
        if self._needs_portal(capture_mode):
            # Wayland: the compositor must hand us a stream before we can
            # capture anything, and that handshake is asynchronous.
            self._pending_path = path
            self._pending_capture_mode = capture_mode
            self._open_portal()
            return
        self._start_capture(path, capture_mode)

    def _needs_portal(self, capture_mode: CaptureMode) -> bool:
        if capture_mode is CaptureMode.AUDIO_ONLY:
            return False
        from .screencast import use_portal_capture
        return use_portal_capture()

    def _open_portal(self) -> None:
        from .screencast import (CURSOR_EMBEDDED, CURSOR_HIDDEN,
                                 ScreenCastSession, source_types_for)
        # No "preparing" notification: the portal puts its own dialog on screen,
        # which is a clearer prompt than anything we could add next to it.
        self._session = ScreenCastSession()
        self._session.open(source_types_for(self.cfg.video_source),
                           self.cfg.wayland_restore_token,
                           self._on_portal_ready, self._on_portal_error,
                           cursor_mode=(CURSOR_EMBEDDED if self.cfg.show_cursor
                                        else CURSOR_HIDDEN))

    def _on_portal_ready(self, session) -> None:
        if self.state is not State.RECORDING or self._pending_path is None:
            session.close()  # the call ended while the dialog was still up
            return
        if session.restore_token:
            from .config import save_restore_token
            save_restore_token(session.restore_token)
        self.recorder.attach_session(session)
        path, self._pending_path = self._pending_path, None
        capture_mode, self._pending_capture_mode = self._pending_capture_mode, None
        assert capture_mode is not None
        self._start_capture(path, capture_mode)

    def _on_portal_error(self, message: str) -> None:
        if self.state is not State.RECORDING or self._pending_path is None:
            return
        LOG.warning("Screen capture unavailable (%s); recording audio only",
                    message)
        self._session = None
        path, self._pending_path = self._pending_path, None
        capture_mode, self._pending_capture_mode = self._pending_capture_mode, None
        assert capture_mode is not None
        self._start_capture(path, capture_mode)

    def _start_capture(self, path: Path, capture_mode: CaptureMode) -> None:
        try:
            started = self.recorder.start(path, self._app or "Meeting", capture_mode)
        except Exception:
            LOG.exception("Recorder start failed")
            started = False
        if not started:
            self._reserved_paths.discard(path)
            self._close_portal()
            self.state = State.IDLE
            self._app = None
            return
        self.state = State.RECORDING
        self._show_widget()  # no-op when _begin_recording already showed it

    def _finish_recording(self, trim_end: float = 0.0) -> bool:
        self._close_widget()
        if not self.recorder.is_recording:
            # The call ended while the portal dialog was still up.
            pending_path = self._pending_path
            self._close_portal()
            if pending_path is not None:
                self._reserved_paths.discard(pending_path)
            return False
        # min_recording_seconds exists to drop false-positive meeting detections;
        # a manual `record` was asked for explicitly, so it always saves.
        too_short = (not self._manual and
                     self.recorder.elapsed() - trim_end < self.cfg.min_recording_seconds)
        if too_short:
            handle = self.recorder.stop(discard=True)
            self._close_portal()
            LOG.info("Discarded: call was shorter than min_recording_seconds")
            self._register_handle(handle)
            return handle is not None
        handle = self.recorder.stop(trim_end=trim_end)
        # After stop(): capture is torn down, so the portal stream is free to go.
        self._close_portal()
        self._register_handle(handle)
        return handle is not None

    def _close_portal(self) -> None:
        """Release the ScreenCast session so the compositor stops the stream."""
        session, self._session = self._session, None
        self._pending_path = None
        self._pending_capture_mode = None
        self.recorder.attach_session(None)
        if session is not None:
            session.close()

    def _register_handle(self, handle: FinalizationHandle | None) -> None:
        if handle is None:
            return
        self._handles.add(handle)
        try:
            from gi.repository import GLib
            GLib.timeout_add(1000, lambda: self._poll_handle(handle))
        except Exception:
            self._dispatch_handle(handle, handle.wait())

    def _poll_handle(self, handle: FinalizationHandle) -> bool:
        done, completed = handle.poll()
        if not done:
            return True
        self._dispatch_handle(handle, completed)
        return False

    def _dispatch_handle(self, handle: FinalizationHandle,
                         completed: CompletedRecording | None) -> None:
        """Remove first so stale polls cannot duplicate local completion effects."""
        if handle not in self._handles:
            return
        self._handles.remove(handle)
        self._reserved_paths.discard(handle.target_path)
        # These stay on screen until dismissed: "saved" is clickable (opening the
        # folder), and a failure needs to be seen.
        try:
            if completed is None:
                self.notifier.info("Recording failed", "Could not finalize the file.",
                                   persistent=True)
            else:
                self.notifier.info(
                    "Recording saved", completed.path.name,
                    icon="folder-videos",
                    on_click=lambda p=completed.path: open_folder(p),
                    click_label="📁 Open Folder",
                    persistent=True,
                )
        except Exception:
            LOG.exception("Could not show recording outcome")
        if self.on_finished:
            try:
                self.on_finished(completed)
            except Exception:
                LOG.exception("Recording completion callback failed")

    def _reserve_path(self, path: Path) -> Path:
        candidate, number = path, 2
        while candidate in self._reserved_paths or candidate.exists():
            candidate = path.with_name(f"{path.stem}-{number}{path.suffix}")
            number += 1
        self._reserved_paths.add(candidate)
        return candidate

    # -- recording controls (tray icon, or floating pill fallback) ---------
    def _show_widget(self) -> None:
        if self._widget is not None:
            return  # already on screen; called from both start paths
        self._widget = self._build_controls()
        if self._widget is None:
            return
        try:
            from gi.repository import GLib
            self._widget.show()
            self._timer_source = GLib.timeout_add(500, self._tick_widget)
        except Exception:  # pragma: no cover
            LOG.debug("could not show recording controls", exc_info=True)
            self._widget = None

    def _build_controls(self):
        """Prefer the top-bar tray icon; fall back to the floating pill."""
        kwargs = dict(on_pause=self.recorder.pause,
                      on_resume=self.recorder.resume,
                      on_stop=self._on_widget_stop)
        try:
            from .tray_indicator import RecordingTray

            # Reuse the indicator because its D-Bus object stays registered.
            if self._tray is None:
                self._tray = RecordingTray(**kwargs)

            return self._tray
        except Exception:
            LOG.warning("Tray icon unavailable, falling back to the floating "
                        "pill", exc_info=True)
        try:
            from .recording_widget import RecordingWidget
            return RecordingWidget(**kwargs)
        except Exception:  # no display / GTK missing — record without controls
            LOG.debug("recording controls unavailable", exc_info=True)
            return None

    def _tick_widget(self) -> bool:
        if self.state is not State.RECORDING or self._widget is None:
            return False
        self._widget.update_time(self.recorder.elapsed())
        return True

    def _on_widget_stop(self) -> None:
        if self.state is State.RECORDING:
            LOG.info("User stopped recording from the widget")
            self._finish_recording()
            self.state = State.IDLE

    def _close_widget(self) -> None:
        if self._timer_source is not None:
            try:
                from gi.repository import GLib
                GLib.source_remove(self._timer_source)
            except Exception:  # pragma: no cover
                pass
            self._timer_source = None
        if self._widget is not None:
            self._widget.close()
            self._widget = None

    # -- shutdown ----------------------------------------------------------
    def shutdown(self) -> None:
        if self._pending_path is not None:
            self._reserved_paths.discard(self._pending_path)
            self._close_portal()
        if self.recorder.is_recording:
            self._finish_recording()
        for handle in list(self._handles):
            self._dispatch_handle(handle, handle.wait())
