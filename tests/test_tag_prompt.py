"""Headless unit tests for the native Speakr tag prompt."""

from __future__ import annotations

import importlib
import sys
import types
from typing import Any, Callable, NamedTuple, Sequence
from unittest.mock import patch


class _Accessible:
    def __init__(self) -> None:
        # Retain accessible text for direct assertions.
        self.name = ""
        self.description = ""

    def set_name(self, name: str) -> None:
        self.name = name

    def set_description(self, description: str) -> None:
        self.description = description


class _StyleContext:
    def __init__(self) -> None:
        # Retain applied classes for visual-contract assertions.
        self.classes: list[str] = []

    def add_class(self, name: str) -> None:
        self.classes.append(name)


class _Widget:
    def __init__(self, **kwargs: Any) -> None:
        # Model the shared GTK state used by prompt controls.
        self.signals: dict[str, list] = {}
        self.children: list[object] = []
        self.accessible = _Accessible()
        self.style_context = _StyleContext()
        self.focused = False
        self.label = kwargs.get("label", "")
        self.xalign = kwargs.get("xalign")

    def connect(self, signal: str, callback: Any) -> int:
        # Keep callbacks in registration order like GTK signals.
        self.signals.setdefault(signal, []).append(callback)
        return len(self.signals[signal])

    def emit(self, signal: str, event: Any = None) -> Any:
        result = None

        # Match GTK signal arguments for event and action callbacks.
        for callback in self.signals.get(signal, []):
            result = callback(self, event) if event is not None else callback(self)
        return result

    def get_accessible(self) -> _Accessible:
        return self.accessible

    def get_style_context(self) -> _StyleContext:
        return self.style_context

    def grab_focus(self) -> None:
        self.focused = True

    def set_hexpand(self, value: bool) -> None:
        self.hexpand = value

    def set_halign(self, value: Any) -> None:
        self.halign = value

    def set_vexpand(self, value: bool) -> None:
        self.vexpand = value

    def set_size_request(self, width: int, height: int) -> None:
        self.size_request = (width, height)


class _Window(_Widget):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

        # Track window lifecycle calls without opening a display.
        self.destroyed = False
        self.shown = False
        self.presented = False

    def set_title(self, title: str) -> None:
        self.title = title

    def set_modal(self, modal: bool) -> None:
        self.modal = modal

    def set_destroy_with_parent(self, value: bool) -> None:
        self.destroy_with_parent = value

    def set_resizable(self, value: bool) -> None:
        self.resizable = value

    def set_default_size(self, width: int, height: int) -> None:
        self.default_size = (width, height)

    def set_border_width(self, width: int) -> None:
        self.border_width = width

    def add(self, child: object) -> None:
        self.children.append(child)

    def show_all(self) -> None:
        self.shown = True

    def present(self) -> None:
        self.presented = True

    def destroy(self) -> None:
        if self.destroyed:
            return

        # Emit destroy once to reproduce GTK's completion re-entry risk.
        self.destroyed = True
        self.emit("destroy")


class _Box(_Widget):
    def pack_start(self, child: object, *_args: Any) -> None:
        self.children.append(child)

    def pack_end(self, child: object, *_args: Any) -> None:
        self.children.append(child)


class _Label(_Widget):
    def set_line_wrap(self, value: bool) -> None:
        self.line_wrap = value

    def set_max_width_chars(self, value: int) -> None:
        self.max_width_chars = value

    def set_text(self, text: str) -> None:
        self.label = text


class _ScrolledWindow(_Widget):
    def set_policy(self, horizontal: Any, vertical: Any) -> None:
        self.policy = (horizontal, vertical)

    def set_overlay_scrolling(self, value: bool) -> None:
        self.overlay_scrolling = value

    def add(self, child: object) -> None:
        self.children.append(child)


class _CheckButton(_Widget):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

        # Start each fake checkbox in GTK's inactive state.
        self.active = False

    def set_active(self, active: bool) -> None:
        # Emit toggled only when the selected state changes.
        changed = self.active != active
        self.active = active
        if changed:
            self.emit("toggled")

    def get_active(self) -> bool:
        return self.active


class _Button(_Widget):
    pass


class _FakeGLib:
    next_source = 1
    timers: dict[int, Callable[[], bool]] = {}
    removed: list[int] = []

    @classmethod
    def reset(cls) -> None:
        # Give each test an empty deterministic timer registry.
        cls.next_source = 1
        cls.timers = {}
        cls.removed = []

    @classmethod
    def timeout_add_seconds(cls, _seconds: int, callback: Callable[[], bool]) -> int:
        # Store the callback under a stable fake GLib source ID.
        source = cls.next_source
        cls.next_source += 1
        cls.timers[source] = callback
        return source

    @classmethod
    def source_remove(cls, source: int) -> None:
        # Record removal while making later direct stale-callback tests possible.
        cls.removed.append(source)
        cls.timers.pop(source, None)

    @staticmethod
    def idle_add(callback: Callable[[], bool]) -> int:
        # Run focus work immediately in the deterministic headless loop.
        callback()
        return 99


# Provide only the GI surface exercised by the prompt.
Gtk = types.SimpleNamespace(
    Window=_Window,
    Box=_Box,
    Label=_Label,
    ScrolledWindow=_ScrolledWindow,
    CheckButton=_CheckButton,
    Button=_Button,
    WindowType=types.SimpleNamespace(TOPLEVEL="toplevel"),
    Orientation=types.SimpleNamespace(VERTICAL="vertical", HORIZONTAL="horizontal"),
    PolicyType=types.SimpleNamespace(NEVER="never", AUTOMATIC="automatic"),
    Align=types.SimpleNamespace(FILL="fill"),
)
Gdk = types.SimpleNamespace(KEY_Escape=65307)
gi = types.ModuleType("gi")
setattr(gi, "require_version", lambda *_args: None)
repository = types.ModuleType("gi.repository")

# Register each fake namespace under the attributes used by production imports.
setattr(repository, "Gtk", Gtk)
setattr(repository, "Gdk", Gdk)
setattr(repository, "GLib", _FakeGLib)

# Load the production module against deterministic fake GI objects.
with patch.dict(sys.modules, {"gi": gi, "gi.repository": repository}):
    sys.modules.pop("meeting_recorder.tag_prompt", None)
    tag_prompt = importlib.import_module("meeting_recorder.tag_prompt")


class _Tag(NamedTuple):
    tag_id: int
    name: str


TAGS = (_Tag(30, "Research"), _Tag(10, "Customer"), _Tag(20, "Weekly sync"))


def _reset_glib() -> None:
    _FakeGLib.reset()


def _prompt(
    tags: Sequence[_Tag] = TAGS,
    initial: Sequence[int] = (),
    status_text: str | None = None,
) -> tuple[Any, list[tuple[int, ...]], list[bool]]:
    # Keep callback results separate so confirmation and dismissal cannot blur.
    confirmed: list[tuple[int, ...]] = []
    dismissed: list[bool] = []

    # Capture both completion channels for direct exactly-once assertions.
    prompt = tag_prompt.TagPrompt(
        tags,
        tuple(initial),
        confirmed.append,
        lambda: dismissed.append(True),
        status_text=status_text,
    )
    return prompt, confirmed, dismissed


def test_done_commits_zero_one_or_multiple_in_display_order() -> None:
    # Cover empty, single, and multiple selections in source order.
    cases = [([], ()), ([1], (10,)), ([0, 1, 2], (30, 10, 20))]

    # Run each selection size through a fresh prompt and fake timer state.
    for active_indexes, expected in cases:
        _reset_glib()
        prompt, confirmed, dismissed = _prompt()

        # Set the requested combination before using the real Done handler.
        for index in active_indexes:
            prompt.checkboxes[index][1].set_active(True)
        prompt.done_button.emit("clicked")

        # Verify one confirmation and no dismissal for each selection size.
        assert confirmed == [expected]
        assert dismissed == []
        assert prompt.win.destroyed


def test_initial_selection_and_count_use_confirmed_ids() -> None:
    # Build a prompt with confirmed IDs supplied out of display order.
    _reset_glib()
    prompt, _confirmed, _dismissed = _prompt(initial=(20, 30))

    # Verify checks follow IDs while the count reflects active rows.
    assert [checkbox.get_active() for _tag, checkbox in prompt.checkboxes] == [True, False, True]
    assert prompt.status_label.label == "2 tags selected · recording continues"


def test_skip_confirms_empty_tuple() -> None:
    # Start with selections so Skip must actively clear the result.
    _reset_glib()
    prompt, confirmed, dismissed = _prompt(initial=(30, 10))

    # Use the real action signal to confirm an empty tuple.
    prompt.skip_button.emit("clicked")

    # Verify Skip confirms rather than dismisses.
    assert confirmed == [()]
    assert dismissed == []


def test_every_dismissal_action_does_not_commit() -> None:
    # Cover every user and window dismissal route.
    actions = ("not-now", "titlebar", "escape", "close", "destroy")

    # Exercise each non-committing exit through a fresh prompt.
    for action in actions:
        _reset_glib()
        prompt, confirmed, dismissed = _prompt(initial=(30,))

        # Drive the selected native action or window lifecycle signal.
        if action == "not-now":
            prompt.not_now_button.emit("clicked")
        elif action == "titlebar":
            prompt.win.emit("delete-event", object())
        elif action == "escape":
            prompt.win.emit("key-press-event", types.SimpleNamespace(keyval=Gdk.KEY_Escape))
        elif action == "close":
            prompt.close()
        else:
            prompt.win.destroy()

        # Verify each route reports exactly one dismissal and no confirmation.
        assert confirmed == []
        assert dismissed == [True]


def test_untouched_prompt_times_out_once_and_stale_callback_is_safe() -> None:
    # Capture the timeout callback from a newly shown untouched prompt.
    _reset_glib()
    prompt, confirmed, dismissed = _prompt()
    prompt.show()
    source = prompt._timer_source
    callback = _FakeGLib.timers[source]

    # Fire the timeout twice to model an already queued stale callback.
    assert callback() is False
    assert callback() is False

    # Verify stale delivery cannot publish a second completion.
    assert confirmed == []
    assert dismissed == [True]


def test_first_pointer_or_keyboard_interaction_cancels_timeout() -> None:
    cases = [
        ("button-press-event", object()),
        ("key-press-event", types.SimpleNamespace(keyval=65)),
    ]

    # Prove both input families invalidate their opening's captured callback.
    for signal, event in cases:
        _reset_glib()
        prompt, confirmed, dismissed = _prompt()
        prompt.show()
        source = prompt._timer_source
        callback = _FakeGLib.timers[source]

        # Interact with a choice, then replay the stale timeout callback.
        prompt.checkboxes[0][1].emit(signal, event)
        assert source in _FakeGLib.removed
        assert callback() is False

        # Verify interaction alone neither confirms nor dismisses the prompt.
        assert confirmed == []
        assert dismissed == []


def test_completion_callback_is_exactly_once_across_late_events() -> None:
    # Capture lifecycle hooks before completing the prompt normally.
    _reset_glib()
    prompt, confirmed, dismissed = _prompt(initial=(10,))
    prompt.show()
    callback = _FakeGLib.timers[prompt._timer_source]

    # Complete normally before replaying destroy, timeout, and another action.
    prompt.done_button.emit("clicked")
    prompt.win.emit("destroy")
    callback()
    prompt.not_now_button.emit("clicked")

    # Verify all late paths remain fenced after confirmation.
    assert confirmed == [(10,)]
    assert dismissed == []


def test_show_focuses_first_checkbox_and_empty_prompt_focuses_not_now() -> None:
    # Build populated and empty prompts against the same fake loop.
    _reset_glib()
    prompt, _confirmed, _dismissed = _prompt()
    empty_prompt, _empty_confirmed, _empty_dismissed = _prompt(tags=())

    # Let each fake idle callback run the same focus path as GTK's main loop.
    prompt.show()
    empty_prompt.show()

    # Verify each prompt chooses its safest initial focus target.
    assert prompt.checkboxes[0][1].focused
    assert empty_prompt.not_now_button.focused


def test_accessibility_and_narrow_layout_contract() -> None:
    # Build the standard prompt for its stable visual and accessible contract.
    _reset_glib()
    prompt, _confirmed, _dismissed = _prompt()

    # Verify the prompt remains non-modal, readable, and narrow-screen safe.
    assert prompt.win.modal is False
    assert prompt.win.accessible.name == "Select Speakr tags"
    assert "does not pause" in prompt.win.accessible.description
    assert prompt.checkboxes[0][1].accessible.name == "Research"
    assert prompt.status_label.accessible.name == "Selected tag count"
    assert prompt.done_button.accessible.description.startswith("Confirm")
    assert prompt.scroller.vexpand is True
    assert prompt.scroller.overlay_scrolling is False
    assert prompt.scroller.size_request == (-1, 120)


def test_optional_source_status_is_concise_and_accessible() -> None:
    # Compare prompts with and without optional source status text.
    _reset_glib()
    prompt, _confirmed, _dismissed = _prompt(
        status_text="Using cached tags from 2026-09-03T12:00:00Z",
    )
    plain_prompt, _plain_confirmed, _plain_dismissed = _prompt()

    # Verify source status is styled and exposed only when supplied.
    assert prompt.source_status_label.label.startswith("Using cached tags")
    assert prompt.source_status_label.accessible.name == "Tag source status"
    assert prompt.source_status_label.accessible.description.endswith("Z")
    assert "dim-label" in prompt.source_status_label.style_context.classes
    assert plain_prompt.source_status_label is None
