"""Ties detector -> notifier -> recorder together as a small state machine.

States: IDLE -> PROMPTING -> RECORDING -> IDLE.
- Meeting detected  : prompt the user (or auto-record if configured).
- User chooses Video or Audio only: start the requested capture mode.
- Meeting ends      : stop + save, notify, return to IDLE.
An ignored session is remembered so we don't re-prompt for the same call.

Notifications cover the capture-mode prompt, a visible audio-only fallback
when requested video is unavailable, and final saved or failure outcomes.
Progress and status are the tray icon's job. "Saved" carries an Open Folder
button: a button has to be clicked, so the file manager never appears unbidden.
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .domain import CaptureMode, CompletedRecording
from .speakr_domain import Tag
from .speakr_tag_service import TagCatalogOutcome, TagCatalogSource
from .notifier import Notifier
from .recorder import FinalizationHandle, Recorder
from .recording_paths import (RecordingPathReservation, collision_safe_path,
                               recording_directory_lock, release_recording_path,
                               reserve_recording_path)
from .utils import LOG, build_output_path, open_folder


class State(enum.Enum):
    IDLE = "idle"
    PROMPTING = "prompting"
    RECORDING = "recording"


class Controller:
    def __init__(
        self,
        cfg: Config,
        notifier: Notifier,
        recorder: Recorder,
        recording_enricher: Callable[..., CompletedRecording] | None = None,
        tag_requester: Callable[[Callable[[TagCatalogOutcome], None]], object | None] | None = None,
        tag_prompt_factory: Callable[..., Any] | None = None,
    ):
        # Keep core capture state separate from optional tag interaction state.
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
        self._reservations: dict[Path, RecordingPathReservation] = {}
        self._active_path: Path | None = None
        self._manual = False         # started by `record`, not by the detector
        self.recording_enricher = recording_enricher

        # Track one recording-scoped catalog and its UI callback generations.
        self._tag_requester = tag_requester
        self._tag_prompt_factory = tag_prompt_factory or self._default_tag_prompt
        self._tag_request_handle = None
        self._tag_request_generation = 0
        self._tag_outcome: TagCatalogOutcome | None = None
        self._tag_catalog: tuple[Tag, ...] = ()
        self._tag_confirmed: tuple[Tag, ...] = ()
        self._tag_prompt = None
        self._tag_prompt_generation = 0
        self._tag_notice_shown = False
        self._handle_tags: dict[FinalizationHandle, tuple[Tag, ...]] = {}

        # Called with the saved Path (or None) once a recording is fully
        # finalized. `record` uses it to know when it can exit.
        self.on_finished: Callable[[CompletedRecording | None], None] | None = None
        self.on_manual_cancelled: Callable[[], None] | None = None

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
            self._begin_recording(CaptureMode.AUDIO_ONLY)
            return
        self.state = State.PROMPTING
        self.notifier.prompt_capture_mode(
            app_name, self.cfg.prompt_timeout_seconds,
            on_video=lambda: self._on_user_capture_mode(CaptureMode.AUDIO_VIDEO),
            on_audio_only=lambda: self._on_user_capture_mode(CaptureMode.AUDIO_ONLY),
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
    def _on_user_capture_mode(self, capture_mode: CaptureMode) -> None:
        if self.state is State.PROMPTING:
            self._begin_recording(capture_mode, discover_tags=True)

    def _on_user_ignore(self) -> None:
        if self.state is State.PROMPTING:
            LOG.info("User declined recording for %s", self._app)
            self._ignored_session = True
            self.state = State.IDLE

    # -- recording helpers -------------------------------------------------
    def _begin_recording(self, capture_mode: CaptureMode | None = None,
                         discover_tags: bool = False) -> None:
        path = build_output_path(self.cfg.output_dir, self._app or "Meeting",
                                 self.cfg.container)
        path = self._reserve_path(path)
        self._active_path = path
        try:
            if capture_mode is None:
                capture_mode = (CaptureMode.AUDIO_VIDEO if self.cfg.record_screen
                                else CaptureMode.AUDIO_ONLY)
            self.state = State.RECORDING
            # Start discovery only for explicit detected-meeting acceptance.
            if discover_tags:
                self._request_tags()
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
        except Exception:
            LOG.exception("Recording setup failed")
            self._end_tag_context()
            self._release_path(path)
            self._active_path = None
            self._close_widget()
            self._close_portal()
            self.state = State.IDLE
            self._app = None

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

    def _on_portal_ready(self, session: Any) -> None:
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

        # Tell the user that recording continues without exposing the portal error.
        try:
            self.notifier.info(
                "Recording audio only",
                "Screen capture was unavailable. Recording continued with audio only.",
            )
        except Exception:
            LOG.exception("Could not show screen capture fallback notice")

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
            self._end_tag_context()
            self._release_path(path)
            self._active_path = None
            self._close_widget()
            self._close_portal()
            self.state = State.IDLE
            self._app = None
            return
        self.state = State.RECORDING
        self._show_widget()  # no-op when _begin_recording already showed it
        self._maybe_show_tag_prompt()

    def _default_tag_prompt(self, **kwargs: Any) -> Any:
        """Lazily create the optional prompt after capture has started."""
        # Keep GTK imports out of manual and headless controller construction.
        from .tag_prompt import TagPrompt

        # Convert confirmed domain tags to the prompt's integer selection IDs.
        kwargs["initial_confirmed"] = tuple(
            tag.tag_id for tag in kwargs["initial_confirmed"]
        )
        return TagPrompt(**kwargs)

    def _request_tags(self) -> None:
        """Start one explicit discovery request for the accepted detected meeting."""
        # A new request fences callbacks from an earlier retry or recording.
        if self._tag_requester is None:
            return
        # Fence callbacks before the requester can synchronously complete.
        self._tag_request_generation += 1
        generation = self._tag_request_generation
        # Reset the per-recording notice budget before its first request or retry.
        if self._tag_outcome is None and not self._tag_catalog:
            self._tag_notice_shown = False
        # Hide the action while the explicit request is unresolved.
        self._set_tag_action(None)
        try:
            self._tag_request_handle = self._tag_requester(
                lambda outcome: self._on_tag_outcome(generation, outcome),
            )
        except Exception:
            self._on_tag_outcome(generation, TagCatalogOutcome(
                (), TagCatalogSource.UNAVAILABLE, None, True,
            ))

    def _on_tag_outcome(self, generation: int, outcome: TagCatalogOutcome) -> None:
        """Store one marshalled result and expose its action only while recording."""
        # Ignore late callbacks after retry, stop, setup failure, or a new recording.
        if generation != self._tag_request_generation or self.state is not State.RECORDING:
            return
        # Retire the completed request before exposing its result to controls.
        self._tag_request_handle = None
        self._tag_outcome = outcome
        if outcome.source in {TagCatalogSource.FRESH, TagCatalogSource.STALE} and outcome.tags:
            self._tag_catalog = outcome.tags
            self._set_tag_action(
                f"Tags ({len(self._tag_confirmed)})" if self._tag_confirmed else "Add tags",
            )
            self._maybe_show_tag_prompt()
            return
        # A successful empty response is a complete catalog, not a retry failure.
        if outcome.source in {TagCatalogSource.FRESH, TagCatalogSource.STALE}:
            self._set_tag_action(None)
            return
        self._set_tag_action("Retry tags")
        if outcome.unavailable_notice and not self._tag_notice_shown:
            self._tag_notice_shown = True
            try:
                self.notifier.info("Tags unavailable", "Tags could not be loaded for this recording.")
            except Exception:
                LOG.debug("Could not show tag discovery notice", exc_info=True)

    def _maybe_show_tag_prompt(self) -> None:
        """Open the tag prompt only after Recorder.start has succeeded."""
        # Portal setup and failed starts never set is_recording, so they cannot prompt.
        if not self.recorder.is_recording or not self._tag_catalog or self._tag_prompt is not None:
            return
        # Fence callbacks from an earlier prompt before opening this one.
        self._tag_prompt_generation += 1
        generation = self._tag_prompt_generation
        # Give the prompt a frozen catalog and callbacks tied to this generation.
        self._tag_prompt = self._tag_prompt_factory(
            tags=self._tag_catalog,
            initial_confirmed=self._tag_confirmed,
            on_confirmed=lambda ids: self._on_tags_confirmed(generation, ids),
            on_dismissed=lambda: self._on_tags_dismissed(generation),
            status_text=self._tag_status_text(),
        )
        self._tag_prompt.show()

    def _tag_status_text(self) -> str:
        """Return bounded source wording without locale-sensitive formatting."""
        # A stale catalog names only its fixed UTC fetch instant.
        if self._tag_outcome and self._tag_outcome.source is TagCatalogSource.STALE:
            fetched = self._tag_outcome.fetched_at_utc
            if fetched is not None:
                return f"Using cached tags from {fetched.isoformat().replace('+00:00', 'Z')[:32]}"
        return "Tags loaded"

    def _on_tags(self) -> None:
        """Reopen frozen tags or request an explicit retry when none are usable."""
        # Reopening a known catalog intentionally does not issue another request.
        if self._tag_catalog:
            self._maybe_show_tag_prompt()
        elif self._tag_request_handle is None:
            self._request_tags()

    def _on_tags_confirmed(self, generation: int, ids: object) -> None:
        """Map selected IDs back to frozen canonical tags in API order."""
        # Only the active prompt can commit a selection.
        if generation != self._tag_prompt_generation or not isinstance(ids, tuple):
            return
        # Preserve API order while reducing confirmed IDs to canonical Tag objects.
        selected = set(ids)
        self._tag_confirmed = tuple(
            tag for tag in self._tag_catalog if tag.tag_id in selected
        )
        # Clear the closed prompt before controls expose its confirmed count.
        self._tag_prompt = None
        self._set_tag_action(f"Tags ({len(self._tag_confirmed)})")

    def _on_tags_dismissed(self, generation: int) -> None:
        """Discard prompt edits while retaining the prior confirmed selection."""
        if generation == self._tag_prompt_generation:
            self._tag_prompt = None

    def _set_tag_action(self, label: str | None) -> None:
        """Apply the stable control seam only when a widget is active."""
        if self._widget is not None and hasattr(self._widget, "set_tag_action"):
            self._widget.set_tag_action(label)

    def _end_tag_context(self) -> tuple[Tag, ...]:
        """Close UI work and freeze confirmed tags for one finalization handle."""
        # Closing the prompt first discards unsaved edits before the recording snapshot.
        if self._tag_prompt is not None:
            try:
                self._tag_prompt.close()
            except Exception:
                pass
        # Invalidate prompt work before cancelling a possible discovery callback.
        self._tag_prompt = None
        self._tag_prompt_generation += 1
        if self._tag_request_handle is not None and hasattr(self._tag_request_handle, "cancel"):
            self._tag_request_handle.cancel()
        self._tag_request_handle = None
        self._tag_request_generation += 1
        # Copy the final confirmed selection before clearing this recording context.
        tags = self._tag_confirmed
        self._tag_catalog = ()
        self._tag_confirmed = ()
        self._tag_outcome = None
        self._tag_notice_shown = False
        return tags

    def _finish_recording(self, trim_end: float = 0.0) -> bool:
        # Freeze tags before stopping so unsaved prompt edits cannot reach a handle.
        confirmed_tags = self._end_tag_context()
        self._close_widget()
        if not self.recorder.is_recording:
            # The call ended while the portal dialog was still up.
            pending_path = self._pending_path
            self._close_portal()
            if pending_path is not None:
                self._release_path(pending_path)
            elif self._active_path is not None:
                self._release_path(self._active_path)
            self._active_path = None
            return False
        # min_recording_seconds exists to drop false-positive meeting detections;
        # a manual `record` was asked for explicitly, so it always saves.
        too_short = (not self._manual and
                     self.recorder.elapsed() - trim_end < self.cfg.min_recording_seconds)
        if too_short:
            try:
                handle = self.recorder.stop(discard=True)
            except Exception:
                LOG.exception("Could not discard recording")
                self._release_path(self._active_path)
                self._active_path = None
                self._close_portal()
                return False
            self._close_portal()
            LOG.info("Discarded: call was shorter than min_recording_seconds")
            self._register_handle(handle, confirmed_tags)
            if handle is None and self._active_path is not None:
                self._release_path(self._active_path)
                self._active_path = None
            return handle is not None
        try:
            handle = self.recorder.stop(trim_end=trim_end)
        except Exception:
            LOG.exception("Could not stop recording")
            self._release_path(self._active_path)
            self._active_path = None
            self._close_portal()
            return False
        # After stop(): capture is torn down, so the portal stream is free to go.
        self._close_portal()
        self._register_handle(handle, confirmed_tags)
        if handle is None and self._active_path is not None:
            self._release_path(self._active_path)
        self._active_path = None
        return handle is not None

    def _close_portal(self) -> None:
        """Release the ScreenCast session so the compositor stops the stream."""
        session, self._session = self._session, None
        self._pending_path = None
        self._pending_capture_mode = None
        self.recorder.attach_session(None)
        if session is not None:
            session.close()

    def _register_handle(self, handle: FinalizationHandle | None,
                         tags: tuple[Tag, ...] = ()) -> None:
        if handle is None:
            return
        self._handles.add(handle)
        self._handle_tags[handle] = tags
        try:
            from gi.repository import GLib
            GLib.timeout_add(1000, lambda: self._poll_handle(handle))
        except Exception:
            # Blocking here is the non-GLib fallback, but completion still goes
            # through the same exactly-once dispatch path as timer polling.
            try:
                completed = handle.wait()
            except Exception:
                LOG.exception("Could not wait for recording finalization")
                completed = handle.abort()
            self._dispatch_handle(handle, completed)

    def _poll_handle(self, handle: FinalizationHandle) -> bool:
        try:
            done, completed = handle.poll()
        except Exception:
            LOG.exception("Could not poll recording finalization")
            completed = handle.abort()
            self._dispatch_handle(handle, completed)
            return False
        if not done:
            return True
        self._dispatch_handle(handle, completed)
        return False

    def _dispatch_handle(self, handle: FinalizationHandle,
                         completed: CompletedRecording | None) -> None:
        """Remove first so stale polls cannot duplicate local completion effects."""
        if handle not in self._handles:
            return
        if completed is not None and self.recording_enricher is not None:
            try:
                enriched = self.recording_enricher(
                    completed, tags=self._handle_tags.get(handle, ()),
                )
                if (not isinstance(enriched, CompletedRecording)
                        or not isinstance(enriched.path, Path)):
                    raise TypeError("recording enricher returned an invalid result")
                completed = enriched
            except Exception:
                LOG.exception("Recording enrichment failed; using the original result")
        self._handles.remove(handle)
        self._handle_tags.pop(handle, None)
        self._release_path(handle.target_path)
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
        with recording_directory_lock(path.parent):
            candidate = collision_safe_path(path)
            number = 2
            while True:
                while candidate in self._reserved_paths:
                    candidate = path.with_name(f"{path.stem}-{number}{path.suffix}")
                    number += 1
                try:
                    reservation = reserve_recording_path(candidate)
                except FileExistsError:
                    candidate = collision_safe_path(candidate)
                    continue
                self._reservations[candidate] = reservation
                self._reserved_paths.add(candidate)
                return candidate

    def _release_path(self, path: Path | None) -> None:
        if path is None:
            return
        reservation = self._reservations.pop(path, None)
        self._reserved_paths.discard(path)
        if reservation is not None:
            release_recording_path(reservation)

    # -- recording controls (tray icon, or floating pill fallback) ---------
    def _show_widget(self) -> None:
        if self._widget is not None:
            return  # already on screen; called from both start paths
        self._widget = self._build_controls()
        if self._widget is None:
            return
        # Apply an outcome that may have arrived before controls were constructed.
        if self._tag_requester is not None:
            if self._tag_request_handle is not None:
                self._set_tag_action(None)
            elif self._tag_catalog:
                self._set_tag_action(
                    f"Tags ({len(self._tag_confirmed)})" if self._tag_confirmed else "Add tags",
                )
            elif self._tag_outcome is not None:
                self._set_tag_action("Retry tags")
        try:
            from gi.repository import GLib
            self._widget.show()
            self._timer_source = GLib.timeout_add(500, self._tick_widget)
        except Exception:  # pragma: no cover
            LOG.debug("could not show recording controls", exc_info=True)
            self._widget = None

    def _build_controls(self) -> Any | None:
        """Prefer the top-bar tray icon; fall back to the floating pill."""
        kwargs = dict(on_pause=self.recorder.pause,
                      on_resume=self.recorder.resume,
                      on_stop=self._on_widget_stop,
                      on_tags=self._on_tags)
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
            started = self._finish_recording()
            self.state = State.IDLE
            # A portal request has no recording handle or completion callback;
            # tell the manual CLI to leave its loop after cancelling it.
            if self._manual and not started and self.on_manual_cancelled:
                self.on_manual_cancelled()

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
            self._release_path(self._pending_path)
            self._close_portal()
        if self.recorder.is_recording:
            self._finish_recording()
        elif self._active_path is not None:
            self._release_path(self._active_path)
            self._active_path = None
        for handle in list(self._handles):
            try:
                completed = handle.wait()
            except Exception:
                LOG.exception("Could not wait for recording finalization")
                completed = handle.abort()
            self._dispatch_handle(handle, completed)
