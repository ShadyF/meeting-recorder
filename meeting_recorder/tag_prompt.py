"""Non-modal GTK prompt for selecting existing Speakr tags during capture."""

from __future__ import annotations

from typing import Any, Callable, Protocol, Sequence

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402


class TagOption(Protocol):
    """Read-only tag data accepted by :class:`TagPrompt`."""

    @property
    def tag_id(self) -> int: ...

    @property
    def name(self) -> str: ...


ConfirmedCallback = Callable[[tuple[int, ...]], None]
DismissedCallback = Callable[[], None]


class TagPrompt:
    """Show an optional tag checklist without blocking recording controls."""

    def __init__(
        self,
        tags: Sequence[TagOption],
        initial_confirmed: tuple[int, ...],
        on_confirmed: ConfirmedCallback,
        on_dismissed: DismissedCallback,
        *,
        status_text: str | None = None,
        timeout_seconds: int = 15,
    ) -> None:
        # Keep immutable prompt inputs and lifecycle state together.
        self.tags = tuple(tags)
        self.initial_confirmed = tuple(initial_confirmed)
        self.on_confirmed = on_confirmed
        self.on_dismissed = on_dismissed
        self.status_text = status_text
        self.timeout_seconds = timeout_seconds
        self._completed = False
        self._shown = False
        self._interacted = False
        self._timer_source: int | None = None
        self._timer_generation = 0

        # Create a separate utility window that never makes recorder controls modal.
        self.win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.win.set_title("Select Speakr tags")
        self.win.set_modal(False)
        self.win.set_destroy_with_parent(False)
        self.win.set_resizable(True)
        self.win.set_default_size(440, 420)
        self.win.set_size_request(320, 260)
        self.win.set_border_width(12)
        self.win.connect("delete-event", self._on_delete)
        self.win.connect("destroy", self._on_destroy)
        self._watch_interaction(self.win)
        self._set_accessibility(
            self.win,
            "Select Speakr tags",
            "Recording is active. Tag selection does not pause or stop capture.",
        )

        # Keep context and the selected count above and below the scrolling checklist.
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.win.add(outer)

        title = Gtk.Label(label="Add tags while recording", xalign=0)
        title.get_style_context().add_class("title")
        outer.pack_start(title, False, False, 0)

        context = Gtk.Label(
            label=(
                "Recording continues. Select any existing tags, or leave all "
                "unchecked."
            ),
            xalign=0,
        )
        context.set_line_wrap(True)
        context.set_max_width_chars(52)
        outer.pack_start(context, False, False, 0)
        self._set_accessibility(
            context,
            "Recording continues",
            "Selecting or dismissing tags does not affect the active recording.",
        )

        # State the catalog source once without competing with the recording context.
        self.source_status_label = None
        if self.status_text:
            self.source_status_label = Gtk.Label(label=self.status_text, xalign=0)
            self.source_status_label.set_line_wrap(True)
            self.source_status_label.set_max_width_chars(52)
            self.source_status_label.get_style_context().add_class("dim-label")
            outer.pack_start(self.source_status_label, False, False, 0)
            self._set_accessibility(
                self.source_status_label,
                "Tag source status",
                self.status_text,
            )

        # Put only the checklist in the scroller so status and actions always remain visible.
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_overlay_scrolling(False)
        self.scroller.set_vexpand(True)
        self.scroller.set_size_request(-1, 120)
        outer.pack_start(self.scroller, True, True, 0)

        tag_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.scroller.add(tag_list)
        self.checkboxes: list[tuple[TagOption, Any]] = []
        initial_ids = set(self.initial_confirmed)

        # Preserve the supplied order when building rows and later confirming IDs.
        for tag in self.tags:
            checkbox = Gtk.CheckButton(label=tag.name)
            checkbox.set_active(tag.tag_id in initial_ids)
            checkbox.set_hexpand(True)
            checkbox.set_halign(Gtk.Align.FILL)
            checkbox.connect("toggled", self._on_selection_changed)
            self._watch_interaction(checkbox)
            self._set_accessibility(
                checkbox,
                tag.name,
                f"Select the existing Speakr tag {tag.name}.",
            )
            tag_list.pack_start(checkbox, False, False, 0)
            self.checkboxes.append((tag, checkbox))

        # Explain an empty list without replacing the stable action area.
        if not self.checkboxes:
            empty = Gtk.Label(
                label="No existing Speakr tags are available. Recording continues.",
                xalign=0,
            )
            empty.set_line_wrap(True)
            tag_list.pack_start(empty, False, False, 8)
            self._set_accessibility(
                empty,
                "No existing Speakr tags",
                "There are no tags to select and recording continues.",
            )

        # Keep the current selection count visible outside the scrolling list.
        self.status_label = Gtk.Label(label="", xalign=0)
        outer.pack_start(self.status_label, False, False, 0)
        self._set_accessibility(
            self.status_label,
            "Selected tag count",
            "The number of tags currently selected in this prompt.",
        )
        self._update_status()

        # Keep all decisions in one fixed row below the scrolling tag list.
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        outer.pack_end(actions, False, False, 0)

        self.skip_button = Gtk.Button(label="Skip · use no tags")
        self.skip_button.connect("clicked", self._on_skip)
        self._watch_interaction(self.skip_button)
        self._set_accessibility(
            self.skip_button,
            "Skip and use no tags",
            "Confirm this recording with no Speakr tags.",
        )
        actions.pack_start(self.skip_button, False, False, 0)

        self.done_button = Gtk.Button(label="Done")
        self.done_button.get_style_context().add_class("suggested-action")
        self.done_button.connect("clicked", self._on_done)
        self._watch_interaction(self.done_button)
        self._set_accessibility(
            self.done_button,
            "Done",
            "Confirm the currently selected Speakr tags.",
        )
        actions.pack_end(self.done_button, False, False, 0)

        self.not_now_button = Gtk.Button(label="Not now")
        self.not_now_button.connect("clicked", self._on_not_now)
        self._watch_interaction(self.not_now_button)
        self._set_accessibility(
            self.not_now_button,
            "Not now",
            "Dismiss tag selection without changing confirmed tags.",
        )
        actions.pack_end(self.not_now_button, False, False, 0)

    def show(self) -> None:
        """Open the prompt and start its untouched timeout once."""
        # Ignore repeated show calls after opening or completion.
        if self._shown or self._completed:
            return

        # Show before focusing so GTK can focus a realized child widget.
        self._shown = True
        self.win.show_all()
        self.win.present()
        self._schedule_timeout()
        GLib.idle_add(self._focus_initial)

    def close(self) -> None:
        """Dismiss the prompt without committing the current checklist."""
        self._finish(None)

    def _focus_initial(self) -> bool:
        # Focus the first choice, or the safe dismissal action for an empty list.
        target = self.checkboxes[0][1] if self.checkboxes else self.not_now_button
        target.grab_focus()
        return False

    def _selected_ids(self) -> tuple[int, ...]:
        return tuple(
            tag.tag_id for tag, checkbox in self.checkboxes if checkbox.get_active()
        )

    def _update_status(self) -> None:
        # Use normal singular and plural wording for assistive output.
        count = len(self._selected_ids())
        noun = "tag" if count == 1 else "tags"
        self.status_label.set_text(f"{count} {noun} selected · recording continues")

    def _schedule_timeout(self) -> None:
        # Capture a generation so a removed callback cannot close a later state.
        self._timer_generation += 1
        generation = self._timer_generation
        self._timer_source = GLib.timeout_add_seconds(
            self.timeout_seconds,
            lambda: self._on_timeout(generation),
        )

    def _cancel_timeout(self) -> None:
        # Invalidate queued callbacks before asking GLib to remove the source.
        self._timer_generation += 1
        source = self._timer_source
        self._timer_source = None
        if source is not None:
            GLib.source_remove(source)

    def _mark_interacted(self) -> None:
        # Only the first real interaction needs to cancel this opening's timer.
        if self._interacted:
            return
        self._interacted = True
        self._cancel_timeout()

    def _watch_interaction(self, widget: Any) -> None:
        # Watch both input families because either one means the prompt is not untouched.
        widget.connect("button-press-event", self._on_pointer_interaction)
        widget.connect("key-press-event", self._on_key_press)

    def _on_pointer_interaction(self, _widget: object, _event: object) -> bool:
        # A pointer event makes this opening ineligible for timeout.
        self._mark_interacted()
        return False

    def _on_key_press(self, _widget: object, event: object) -> bool:
        # Cancel timeout for every key, then reserve Escape for dismissal.
        self._mark_interacted()
        if getattr(event, "keyval", None) != Gdk.KEY_Escape:
            return False
        self._finish(None)
        return True

    def _on_selection_changed(self, _checkbox: object) -> None:
        # Reflect the changed checklist state in the persistent status line.
        self._update_status()

    def _on_done(self, _button: object) -> None:
        # Commit the selected IDs in their displayed order.
        self._finish(self._selected_ids())

    def _on_skip(self, _button: object) -> None:
        # Commit an explicit empty selection rather than dismissing.
        self._finish(())

    def _on_not_now(self, _button: object) -> None:
        # Dismiss without replacing the last confirmed selection.
        self._finish(None)

    def _on_delete(self, _window: object, _event: object) -> bool:
        # Treat the native titlebar close as a non-committing dismissal.
        self._finish(None)
        return True

    def _on_timeout(self, generation: int) -> bool:
        # Ignore callbacks invalidated by interaction, completion, or source removal.
        if (
            self._completed
            or self._interacted
            or generation != self._timer_generation
        ):
            return False
        self._timer_source = None
        self._finish(None)
        return False

    def _on_destroy(self, _window: object) -> None:
        # Ignore the destroy signal emitted by an exit already in progress.
        if self._completed:
            return

        # Treat an external destroy as one dismissal without destroying again.
        self._completed = True
        self._cancel_timeout()
        self.on_dismissed()

    def _finish(self, confirmed: tuple[int, ...] | None) -> None:
        # Fence every late action and lifecycle callback after the first result.
        if self._completed:
            return

        # Mark completion before destroy emits so all exit paths stay exactly once.
        self._completed = True
        self._cancel_timeout()
        self.win.destroy()

        # Publish confirmation separately from every non-committing dismissal path.
        if confirmed is None:
            self.on_dismissed()
        else:
            self.on_confirmed(confirmed)

    @staticmethod
    def _set_accessibility(widget: Any, name: str, description: str) -> None:
        # Set explicit ATK text because visual context may not be read with controls.
        accessible = widget.get_accessible()
        accessible.set_name(name)
        accessible.set_description(description)
