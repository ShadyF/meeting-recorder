"""Headless tests for optional tag actions in both recording controls."""

from __future__ import annotations

import importlib
import sys
import types
from typing import Any
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


class _Style:
    def __init__(self) -> None:
        # Retain applied classes for visual-contract assertions.
        self.classes: list[str] = []

    def add_class(self, name: str) -> None:
        self.classes.append(name)


class _Widget:
    def __init__(self, **kwargs: Any) -> None:
        # Model the shared GTK state used by both recording controls.
        self.label = kwargs.get("label", "")
        self.children: list[Any] = []
        self.signals: dict[str, list] = {}
        self.visible = False
        self.no_show_all = False
        self.sensitive = True
        self.accessible = _Accessible()
        self.style = _Style()

    def connect(self, signal: str, callback: Any) -> None:
        # Keep callbacks in registration order like GTK signals.
        self.signals.setdefault(signal, []).append(callback)

    def emit(self, signal: str) -> None:
        # Dispatch the same widget argument GTK sends for menu and button actions.
        for callback in self.signals.get(signal, []):
            callback(self)

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False

    def show_all(self) -> None:
        # Respect explicit dynamic visibility on no-show-all widgets.
        if not self.no_show_all:
            self.visible = True

        # Reproduce recursive GTK visibility while respecting no-show-all children.
        for child in self.children:
            child.show_all()

    def set_no_show_all(self, value: bool) -> None:
        self.no_show_all = value

    def set_sensitive(self, value: bool) -> None:
        self.sensitive = value

    def set_label(self, label: str) -> None:
        self.label = label

    def set_tooltip_text(self, text: str) -> None:
        self.tooltip = text

    def set_opacity(self, value: float) -> None:
        self.opacity = value

    def get_accessible(self) -> _Accessible:
        return self.accessible

    def get_style_context(self) -> _Style:
        return self.style


class _Window(_Widget):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

        # Track window lifecycle and its fake display surface.
        self.destroyed = False
        self.screen = _Screen()

    def set_decorated(self, value: bool) -> None:
        self.decorated = value

    def set_keep_above(self, value: bool) -> None:
        self.keep_above = value

    def set_skip_taskbar_hint(self, value: bool) -> None:
        self.skip_taskbar = value

    def set_skip_pager_hint(self, value: bool) -> None:
        self.skip_pager = value

    def set_resizable(self, value: bool) -> None:
        self.resizable = value

    def set_type_hint(self, value: Any) -> None:
        self.type_hint = value

    def set_accept_focus(self, value: bool) -> None:
        self.accept_focus = value

    def set_focus_on_map(self, value: bool) -> None:
        self.focus_on_map = value

    def set_app_paintable(self, value: bool) -> None:
        self.app_paintable = value

    def get_screen(self) -> "_Screen":
        return self.screen

    def set_visual(self, visual: Any) -> None:
        self.visual = visual

    def add(self, child: object) -> None:
        self.children.append(child)

    def destroy(self) -> None:
        self.destroyed = True


class _Box(_Widget):
    def pack_start(self, child: object, *_args: Any) -> None:
        self.children.append(child)


class _Menu(_Widget):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

        # Count resize requests caused by dynamic menu sections.
        self.resize_count = 0

    def append(self, child: object) -> None:
        self.children.append(child)

    def queue_resize(self) -> None:
        self.resize_count += 1


class _Screen:
    def get_rgba_visual(self) -> str:
        return "rgba"


class _CssProvider:
    def load_from_data(self, data: bytes) -> None:
        self.data = data


class _GlobalStyleContext:
    @staticmethod
    def add_provider_for_screen(*_args: Any) -> None:
        pass


class _IconTheme:
    @staticmethod
    def get_default() -> "_IconTheme":
        return _IconTheme()

    def has_icon(self, _name: str) -> bool:
        return False


class _Indicator:
    def __init__(self) -> None:
        # Retain panel state for direct integration assertions.
        self.identifier = ""
        self.icon = ""
        self.category = None

    @classmethod
    def new(cls, identifier: str, icon: str, category: Any) -> "_Indicator":
        # Preserve constructor values for menu integration assertions.
        indicator = cls()
        indicator.identifier = identifier
        indicator.icon = icon
        indicator.category = category
        return indicator

    def set_status(self, status: Any) -> None:
        self.status = status

    def set_menu(self, menu: _Menu) -> None:
        self.menu = menu

    def set_label(self, label: str, guide: str) -> None:
        self.label = (label, guide)

    def set_icon_full(self, icon: str, description: str) -> None:
        self.icon = icon
        self.icon_description = description


class _GLib:
    removed: list[int] = []

    @staticmethod
    def idle_add(_callback: Any) -> int:
        return 1

    @staticmethod
    def timeout_add(_milliseconds: int, _callback: Any) -> int:
        return 2

    @classmethod
    def source_remove(cls, source: int) -> None:
        cls.removed.append(source)


# Provide only the GI surface exercised by both recording controls.
Gtk = types.SimpleNamespace(
    Window=_Window,
    Box=_Box,
    Label=_Widget,
    Button=_Widget,
    Menu=_Menu,
    MenuItem=_Widget,
    SeparatorMenuItem=_Widget,
    CssProvider=_CssProvider,
    StyleContext=_GlobalStyleContext,
    IconTheme=_IconTheme,
    WindowType=types.SimpleNamespace(TOPLEVEL="toplevel"),
    Orientation=types.SimpleNamespace(HORIZONTAL="horizontal"),
    STYLE_PROVIDER_PRIORITY_APPLICATION=600,
)
Gdk = types.SimpleNamespace(WindowTypeHint=types.SimpleNamespace(UTILITY="utility"))
AppIndicator3 = types.SimpleNamespace(
    Indicator=_Indicator,
    IndicatorCategory=types.SimpleNamespace(APPLICATION_STATUS="application"),
    IndicatorStatus=types.SimpleNamespace(PASSIVE="passive", ACTIVE="active"),
)
gi = types.ModuleType("gi")
setattr(gi, "require_version", lambda *_args: None)
repository = types.ModuleType("gi.repository")

# Register each fake namespace under the attributes used by production imports.
setattr(repository, "Gtk", Gtk)
setattr(repository, "Gdk", Gdk)
setattr(repository, "GLib", _GLib)
setattr(repository, "AppIndicator3", AppIndicator3)

# Load both control implementations against the same deterministic GTK surface.
with patch.dict(sys.modules, {"gi": gi, "gi.repository": repository}):
    sys.modules.pop("meeting_recorder.recording_widget", None)
    sys.modules.pop("meeting_recorder.tray_indicator", None)
    recording_widget = importlib.import_module("meeting_recorder.recording_widget")
    tray_indicator = importlib.import_module("meeting_recorder.tray_indicator")


def _callbacks() -> tuple[list[str], dict[str, Any]]:
    # Route every control callback into one ordered event list.
    events: list[str] = []
    return events, {
        "on_pause": lambda: events.append("pause"),
        "on_resume": lambda: events.append("resume"),
        "on_stop": lambda: events.append("stop"),
        "on_tags": lambda: events.append("tags"),
    }


def test_floating_tag_action_updates_after_show_without_weakening_controls() -> None:
    # Build the floating control with every callback available.
    events, callbacks = _callbacks()
    widget = recording_widget.RecordingWidget(**callbacks)

    # Opening controls must not reveal the tag action while discovery is pending.
    widget.show()
    assert not widget.tag_btn.visible
    assert widget.pause_btn.visible and widget.stop_btn.visible
    assert widget.win.accept_focus and not widget.win.focus_on_map

    # Dynamic labels remain accessible and invoke only their optional callback.
    widget.set_tag_action("Add tags")
    assert widget.tag_btn.visible and widget.tag_btn.sensitive
    assert widget.tag_btn.accessible.name == "Add tags"
    widget.tag_btn.emit("clicked")
    widget.pause_btn.emit("clicked")
    widget.stop_btn.emit("clicked")
    assert events == ["tags", "pause", "stop"]

    # Hiding blocks synthetic stale activation as well as pointer access.
    widget.set_tag_action(None)
    widget.tag_btn.emit("clicked")
    assert not widget.tag_btn.visible and not widget.tag_btn.sensitive
    assert events == ["tags", "pause", "stop"]


def test_floating_tag_action_supports_retry_count_and_missing_callback() -> None:
    # Build the floating control for dynamic label updates.
    events, callbacks = _callbacks()
    widget = recording_widget.RecordingWidget(**callbacks)

    # Update the same compact button rather than adding competing controls.
    widget.set_tag_action("Retry tags")
    assert widget.tag_btn.label == "Retry tags"
    widget.set_tag_action("Tags (2)")
    assert widget.tag_btn.label == "Tags (2)"
    widget.tag_btn.emit("clicked")
    assert events == ["tags"]

    # Source-compatible construction remains safe when tagging is unavailable.
    plain = recording_widget.RecordingWidget(
        callbacks["on_pause"], callbacks["on_resume"], callbacks["on_stop"])
    plain.set_tag_action("Add tags")
    plain.tag_btn.emit("clicked")
    assert plain.tag_btn.visible and not plain.tag_btn.sensitive


def test_tray_tag_action_updates_after_show_and_resets_on_close() -> None:
    # Build the tray control with every callback available.
    events, callbacks = _callbacks()
    tray = tray_indicator.RecordingTray(**callbacks)

    # The pending state stays absent even after the whole menu is shown.
    tray.show()
    assert not tray.tag_item.visible and not tray.tag_separator.visible
    assert tray.pause_item.visible and tray.stop_item.visible

    # Show the optional section and keep recording actions independently active.
    tray.set_tag_action("Tags (1)")
    assert tray.tag_item.visible and tray.tag_separator.visible
    assert tray.tag_item.accessible.name == "Tags (1)"
    tray.tag_item.emit("activate")
    tray.pause_item.emit("activate")
    tray.stop_item.emit("activate")
    assert events == ["tags", "pause", "stop"]

    # A reused AppIndicator must begin the next recording without stale tag state.
    tray.close()
    tray.show()
    assert not tray.tag_item.visible and not tray.tag_separator.visible
    assert tray.ind.status == "active"


def test_tray_retry_hide_and_missing_callback_are_safe() -> None:
    # Build the tray control for retry and unavailable-callback states.
    events, callbacks = _callbacks()
    tray = tray_indicator.RecordingTray(**callbacks)

    # Retry uses the same item, and hiding prevents stale activation.
    tray.set_tag_action("Retry tags")
    assert tray.tag_item.label == "Retry tags"
    tray.set_tag_action(None)
    tray.tag_item.emit("activate")
    assert events == []
    assert not tray.tag_item.visible and not tray.tag_separator.visible

    # A missing optional callback leaves the supplied text visible but disabled.
    plain = tray_indicator.RecordingTray(
        callbacks["on_pause"], callbacks["on_resume"], callbacks["on_stop"])
    plain.set_tag_action("Add tags")
    plain.tag_item.emit("activate")
    assert plain.tag_item.visible and not plain.tag_item.sensitive
