"""Wayland screen capture via the xdg-desktop-portal ScreenCast interface.

There is no Wayland equivalent of x11grab: a client cannot read the screen
directly. Instead the compositor publishes the capture as a PipeWire stream,
handed out by `org.freedesktop.portal.ScreenCast` after the user picks a source
in the portal's own dialog.

ffmpeg has no PipeWire input device, so `recorder` pumps frames with a small
`gst-launch-1.0 pipewiresrc` pipeline and feeds them to ffmpeg as raw video.
This module only does the D-Bus part: get a node id and a file descriptor.

The handshake is `CreateSession` -> `SelectSources` -> `Start`. Each of those
returns a *Request* object path and answers later on its `Response` signal, so
`open()` is asynchronous and needs the GLib main loop the daemon already runs.
`Start` is the step that may show a dialog; passing `persist_mode=2` makes the
portal return a `restore_token` we store in the user config, so subsequent
recordings restore the same source silently and Record stays one click.
"""

from __future__ import annotations

import os
import random
from typing import Callable

from .domain import VideoSource
from .utils import LOG

# `gi` is imported lazily, not at module scope: `use_portal_capture()` is called
# from the recorder's pure command-building path, which the zero-dependency test
# suite exercises on machines without PyGObject installed.


def _dbus():
    """Import the GObject D-Bus bindings on demand."""
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
    return Gio, GLib

_BUS_NAME = "org.freedesktop.portal.Desktop"
_OBJECT_PATH = "/org/freedesktop/portal/desktop"
_SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
_REQUEST_IFACE = "org.freedesktop.portal.Request"

# ScreenCast source types (bitmask) and cursor modes, per the portal spec.
SOURCE_MONITOR = 1
SOURCE_WINDOW = 2
CURSOR_HIDDEN = 1    # compositor leaves the pointer out of the stream
CURSOR_EMBEDDED = 2  # draw the pointer into the frames, like x11grab does
PERSIST_UNTIL_REVOKED = 2

# The handshake can stall indefinitely: the user may never answer the dialog,
# and xdg-desktop-portal-gnome is known to crash mid-request (leaving no reply
# at all). Give up rather than sit in PROMPTING forever with nothing recording.
HANDSHAKE_TIMEOUT_SECONDS = 120


def use_portal_capture() -> bool:
    """True when video must come from the portal instead of x11grab.

    That is the case on Wayland, where a client simply cannot read the screen.
    MEETING_RECORDER_CAPTURE=portal|x11 forces either backend — the portal path
    also works on GNOME/X11, which makes it testable without switching sessions.
    """
    forced = os.environ.get("MEETING_RECORDER_CAPTURE", "").lower()
    if forced in ("portal", "x11"):
        return forced == "portal"
    return is_wayland()


def is_wayland() -> bool:
    """True when this is a Wayland session.

    Checked before DISPLAY on purpose: XWayland exports DISPLAY too, so a
    DISPLAY test alone would wrongly report X11 on a Wayland desktop.
    """
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return True
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def _unique_token(prefix: str) -> str:
    """Handle token for a portal Request/Session (must be a valid path element)."""
    return f"{prefix}_{os.getpid()}_{random.randrange(1 << 30)}"


class ScreenCastError(Exception):
    """The portal refused, failed, or is unavailable."""


class ScreenCastSession:
    """One portal ScreenCast session, held open for a whole recording.

    Opening is async (`open()`); once ready, `node_id` and `size` describe the
    stream and `open_fd()` returns a fresh PipeWire fd for each capture segment.
    """

    def __init__(self) -> None:
        self._bus = None
        self._session_path: str | None = None
        self.node_id: int | None = None
        self.size: tuple[int, int] | None = None
        self.restore_token: str = ""
        self._on_ready: Callable[[ScreenCastSession], None] | None = None
        self._on_error: Callable[[str], None] | None = None
        self._source_types: int = SOURCE_MONITOR
        self._requested_token: str = ""
        self._cursor_mode: int = CURSOR_EMBEDDED
        self._timeout_id: int | None = None

    @property
    def is_open(self) -> bool:
        return self.node_id is not None

    # -- handshake ---------------------------------------------------------
    def open(self, source_types: int, restore_token: str,
             on_ready: Callable[["ScreenCastSession"], None],
             on_error: Callable[[str], None],
             cursor_mode: int = CURSOR_EMBEDDED) -> None:
        """Start the async CreateSession -> SelectSources -> Start handshake.

        Exactly one of `on_ready` / `on_error` is called, from the GLib loop.
        Unlike X11, the pointer cannot be added or removed after the fact here:
        the compositor decides, so `cursor_mode` has to be set up front.
        """
        self._on_ready = on_ready
        self._on_error = on_error
        self._source_types = source_types
        self._requested_token = restore_token
        self._cursor_mode = cursor_mode
        try:
            Gio, GLib = _dbus()
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except ImportError as exc:
            self._fail(f"PyGObject is unavailable: {exc}")
            return
        except Exception as exc:  # GLib.Error - not importable at module scope
            self._fail(f"no session bus: {exc}")
            return
        self._timeout_id = GLib.timeout_add_seconds(
            HANDSHAKE_TIMEOUT_SECONDS, self._on_timeout)
        self._call("CreateSession", "(a{sv})", [],
                   {"session_handle_token": GLib.Variant("s", _unique_token("mr_ses"))},
                   self._on_session_created)

    def _call(self, method: str, signature: str, args: list,
              options: dict, handler: Callable[[dict], None]) -> None:
        """Invoke a ScreenCast method and route its Response signal to `handler`.

        `args` are the parameters before the trailing options dict, which differ
        per method (CreateSession takes none, SelectSources the session handle,
        Start the session handle plus a parent-window identifier).

        The reply carries the Request object path, but the portal may emit the
        Response signal before that reply arrives — so subscribe first, using
        the path we can predict from our own handle token, and unsubscribe when
        it fires.
        """
        assert self._bus is not None
        Gio, GLib = _dbus()
        token = _unique_token("mr_req")
        options = dict(options)
        options["handle_token"] = GLib.Variant("s", token)
        sender = self._bus.get_unique_name()[1:].replace(".", "_")
        request_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

        sub_id = 0

        def _on_response(_conn, _sender, _path, _iface, _signal, params):
            self._bus.signal_unsubscribe(sub_id)
            code, results = params.unpack()
            if code != 0:  # 1 = user cancelled, 2 = ended some other way
                self._fail(f"{method} was denied or cancelled (code {code})")
                return
            try:
                handler(results)
            except ScreenCastError as exc:
                self._fail(str(exc))

        sub_id = self._bus.signal_subscribe(
            _BUS_NAME, _REQUEST_IFACE, "Response", request_path, None,
            Gio.DBusSignalFlags.NONE, _on_response)

        def _on_reply(bus, res):
            try:
                bus.call_finish(res)
            except GLib.Error as exc:
                self._bus.signal_unsubscribe(sub_id)
                self._fail(f"{method} failed: {exc.message}")

        params = GLib.Variant(signature, tuple(args) + (options,))
        self._bus.call(
            _BUS_NAME, _OBJECT_PATH, _SCREENCAST_IFACE, method, params,
            GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, -1, None, _on_reply)

    def _on_session_created(self, results: dict) -> None:
        self._session_path = results.get("session_handle")
        if not self._session_path:
            raise ScreenCastError("portal returned no session handle")
        _, GLib = _dbus()
        options = {
            "types": GLib.Variant("u", self._source_types),
            "multiple": GLib.Variant("b", False),
            "cursor_mode": GLib.Variant("u", self._cursor_mode),
            "persist_mode": GLib.Variant("u", PERSIST_UNTIL_REVOKED),
        }
        if self._requested_token:
            options["restore_token"] = GLib.Variant("s", self._requested_token)
        self._call("SelectSources", "(oa{sv})", [self._session_path], options,
                   self._on_sources_selected)

    def _on_sources_selected(self, _results: dict) -> None:
        # Empty parent window: the daemon has no window to parent the dialog to.
        self._call("Start", "(osa{sv})", [self._session_path, ""], {},
                   self._on_started)

    def _on_started(self, results: dict) -> None:
        streams = results.get("streams") or []
        if not streams:
            raise ScreenCastError("portal returned no streams")
        node_id, props = streams[0]
        self.node_id = int(node_id)
        size = props.get("size")
        if size:
            self.size = (int(size[0]), int(size[1]))
        self.restore_token = results.get("restore_token", "") or ""
        self._cancel_timeout()
        LOG.info("ScreenCast ready: node %s, size %s", self.node_id, self.size)
        if self._on_ready:
            self._on_ready(self)

    def _on_timeout(self) -> bool:
        self._timeout_id = None
        self._fail("the screen-sharing dialog was never answered "
                   "(or the portal stopped responding)")
        return False

    def _cancel_timeout(self) -> None:
        if self._timeout_id is not None:
            try:
                _, GLib = _dbus()
                GLib.source_remove(self._timeout_id)
            except Exception:  # pragma: no cover - best effort
                pass
            self._timeout_id = None

    def _fail(self, message: str) -> None:
        self._cancel_timeout()
        LOG.warning("ScreenCast portal: %s", message)
        self.close()
        if self._on_error:
            on_error, self._on_error = self._on_error, None
            self._on_ready = None
            on_error(message)

    # -- use / teardown ----------------------------------------------------
    def open_fd(self) -> int:
        """Return a PipeWire remote fd for this session (caller owns it).

        Unlike the handshake this is a plain synchronous call with no dialog,
        so a new segment can grab a fresh fd without asking the user again.
        """
        if self._bus is None or self._session_path is None:
            raise ScreenCastError("session is not open")
        Gio, GLib = _dbus()
        try:
            reply, fd_list = self._bus.call_with_unix_fd_list_sync(
                _BUS_NAME, _OBJECT_PATH, _SCREENCAST_IFACE, "OpenPipeWireRemote",
                GLib.Variant("(oa{sv})", (self._session_path, {})),
                GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, -1, None, None)
        except GLib.Error as exc:
            raise ScreenCastError(f"OpenPipeWireRemote failed: {exc.message}") from exc
        return fd_list.steal_fds()[reply.unpack()[0]]

    def close(self) -> None:
        """Close the portal session; safe to call more than once."""
        self._cancel_timeout()
        bus, path = self._bus, self._session_path
        self._session_path = None
        self.node_id = None
        if bus is None or path is None:
            return
        Gio, _ = _dbus()
        try:
            bus.call_sync(_BUS_NAME, path, "org.freedesktop.portal.Session",
                          "Close", None, None, Gio.DBusCallFlags.NONE, -1, None)
        except Exception as exc:  # GLib.Error
            LOG.debug("closing ScreenCast session failed: %s", exc)


def source_types_for(video_source: VideoSource) -> int:
    """Portal source bitmask for a configured video source.

    'area' has no portal equivalent — we take a monitor and crop it downstream.
    """
    return SOURCE_WINDOW if video_source is VideoSource.WINDOW else SOURCE_MONITOR
