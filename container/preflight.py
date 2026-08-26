"""Validate the session resources required by the container daemon.

The check is deliberately local.  It reads environment values and socket file
metadata, but it never opens a desktop, audio, bus, portal, or secret-service
connection.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import unquote
from zoneinfo import ZoneInfo


_MAX_ERROR_LENGTH = 128
_MAX_ERRORS = 4
_TIMEZONE_SHAPE = re.compile(r"^[^/\s\x00]+(?:/[^/\s\x00]+)+$")
_X11_DISPLAY = re.compile(r"^:(\d+)(?:\.\d+)?$")
_X11_SOCKET_ROOT = Path("/tmp/.X11-unix")


def _runtime_path(runtime_dir: str | None, child: str) -> Path:
    """Build a runtime socket path, rejecting an unusable runtime root."""
    # Fallback sockets must have a real absolute runtime root.
    if not runtime_dir or "\x00" in runtime_dir:
        raise ValueError("runtime directory is missing")

    # Keep fallback sockets under the explicitly supplied runtime directory.
    root = Path(runtime_dir)
    if not root.is_absolute():
        raise ValueError("runtime directory is not absolute")
    return root / child


def _validate_socket_path(path: Path, resource: str) -> str | None:
    """Return a bounded error when a resource is absent or not a Unix socket."""
    # Read socket metadata without opening or probing the service.
    try:
        mode = os.stat(path).st_mode
    except (FileNotFoundError, NotADirectoryError, OSError):
        return f"{resource} socket is unavailable"

    # A listening AF_UNIX socket is represented by the same filesystem type as
    # any other Unix socket; stat is enough and avoids making a service call.
    if not stat.S_ISSOCK(mode):
        return f"{resource} path is not a Unix socket"
    return None


def validate_timezone(value: str | None) -> str | None:
    """Validate the required Region/City timezone without exposing its value."""
    # Reject missing timezone input before consulting the timezone database.
    if not value:
        return "TZ is missing"

    # Reject values that cannot have the required Region/City shape.
    if len(value) > _MAX_ERROR_LENGTH or _TIMEZONE_SHAPE.fullmatch(value) is None:
        return "TZ is malformed"

    # Loading ZoneInfo checks the installed tzdata database as well as syntax.
    try:
        ZoneInfo(value)
    except (KeyError, ValueError, OSError):
        return "TZ is unknown"
    return None


def wayland_socket_path(display: str | None, runtime_dir: str | None) -> Path | None:
    """Resolve WAYLAND_DISPLAY as an absolute or runtime-relative socket."""
    # A missing display is reported by the required-resource validation.
    if display is None:
        return None

    # Reject empty and NUL-containing display names before building a path.
    if not display or "\x00" in display:
        raise ValueError("WAYLAND_DISPLAY is malformed")

    # Wayland accepts a full socket path or a name below XDG_RUNTIME_DIR.
    if display.startswith("/"):
        return Path(display)
    return _runtime_path(runtime_dir, display)


def pulse_socket_path(server: str | None, runtime_dir: str | None) -> Path | None:
    """Resolve a Unix PULSE_SERVER value or the standard runtime fallback."""
    # Use the standard runtime socket when no explicit server is provided.
    if server is None:
        return _runtime_path(runtime_dir, "pulse/native") if runtime_dir else None

    # Only Unix Pulse addresses are safe for this local metadata check.
    if not server.startswith("unix:"):
        raise ValueError("PULSE_SERVER is not a Unix address")

    # Pulse accepts a Unix address after the transport prefix.  A relative
    # address is kept in the runtime directory rather than the working folder.
    socket_name = server[5:]

    # Reject an empty or NUL-containing socket name before path handling.
    if not socket_name or "\x00" in socket_name:
        raise ValueError("PULSE_SERVER is malformed")

    # Preserve absolute Pulse paths and resolve relative names under the runtime root.
    if socket_name.startswith("/"):
        return Path(socket_name)
    return _runtime_path(runtime_dir, socket_name)


def dbus_socket_path(address: str | None, runtime_dir: str | None) -> Path | None:
    """Resolve a filesystem Unix address from DBUS_SESSION_BUS_ADDRESS."""
    # Use the standard session-bus socket when no address is provided.
    if address is None:
        return _runtime_path(runtime_dir, "bus") if runtime_dir else None

    # D-Bus separates transports with semicolons and fields with commas.  Only
    # the path field is used, so GUIDs and other fields are never printed or
    # interpreted as paths.
    for transport in address.split(";"):
        # Ignore non-Unix transports because they cannot name a local socket file.
        if not transport.startswith("unix:"):
            continue

        # Inspect fields independently so GUID and other comma-separated values stay opaque.
        for field in transport[5:].split(","):
            key, separator, encoded_value = field.partition("=")

            # Continue until a filesystem path field is found.
            if key != "path" or not separator or not encoded_value:
                continue
            socket_name = unquote(encoded_value)

            # Accept only absolute, NUL-free filesystem paths.
            if socket_name.startswith("/") and "\x00" not in socket_name:
                return Path(socket_name)
            raise ValueError("DBUS_SESSION_BUS_ADDRESS path is malformed")

    raise ValueError("DBUS_SESSION_BUS_ADDRESS has no filesystem Unix path")


def x11_socket_path(display: str | None, socket_root: Path = _X11_SOCKET_ROOT) -> Path | None:
    """Resolve only the local X11 display form mounted by the container."""
    # Require the exact local display notation to avoid accepting remote displays.
    if not isinstance(display, str):
        return None
    match = _X11_DISPLAY.fullmatch(display)
    if match is None:
        raise ValueError("DISPLAY is malformed")
    return socket_root / f"X{match.group(1)}"


def _validate_xauthority(path_value: str | None) -> str | None:
    """Require a readable, nonempty regular Xauthority file without exposing it."""
    # Treat missing, unsafe, and unusable authority files as one redacted failure.
    if not path_value or "\x00" in path_value:
        return "Xauthority file is unavailable"
    try:
        metadata = os.stat(path_value)
    except OSError:
        return "Xauthority file is unavailable"
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size == 0
            or not metadata.st_mode & 0o444 or not os.access(path_value, os.R_OK)):
        return "Xauthority file is unavailable"
    return None


def _resource_path(
    resolver: Callable[[Mapping[str, str], str | None], Path | None],
    env: Mapping[str, str], resource: str,
) -> tuple[Path | None, str | None]:
    """Run one resolver and convert parser failures to safe resource errors."""
    # Convert all resolver failures into stable resource-only messages.
    try:
        path = resolver(env, env.get("XDG_RUNTIME_DIR"))
    except (TypeError, ValueError, OSError):
        return None, f"{resource} address is malformed"
    return path, None


def validate_environment(
    env: Mapping[str, str] | None = None, *, x11_socket_root: Path = _X11_SOCKET_ROOT,
) -> tuple[str, ...]:
    """Return stable, redacted errors for the daemon's local prerequisites."""
    values = os.environ if env is None else env
    errors: list[str] = []

    # Timezone data is required because recordings and metadata use local time.
    timezone_error = validate_timezone(values.get("TZ"))
    if timezone_error is not None:
        errors.append(timezone_error)

    # Validate only the display system selected by the active desktop session.
    session_type = values.get("XDG_SESSION_TYPE")
    if session_type == "wayland":
        path, error = _resource_path(
            lambda current, runtime: wayland_socket_path(
                current.get("WAYLAND_DISPLAY"), runtime),
            values, "Wayland",
        )
        if error is not None:
            errors.append(error)
        elif path is None:
            errors.append("Wayland socket is unavailable")
        else:
            socket_error = _validate_socket_path(path, "Wayland")
            if socket_error is not None:
                errors.append(socket_error)
    elif session_type == "x11":
        # Map the selected local display to the mounted X11 socket root.
        try:
            path = x11_socket_path(values.get("DISPLAY"), x11_socket_root)
        except ValueError:
            errors.append("X11 display is malformed")
        else:
            if path is None:
                errors.append("X11 display is unavailable")
            else:
                socket_error = _validate_socket_path(path, "X11")
                if socket_error is not None:
                    errors.append(socket_error)
        # Require the matching authority file after validating the display socket.
        xauthority_error = _validate_xauthority(values.get("XAUTHORITY"))
        if xauthority_error is not None:
            errors.append(xauthority_error)
    else:
        errors.append("desktop session type is unsupported")

    # PulseAudio always uses its explicit Unix address or the runtime fallback.
    path, error = _resource_path(
        lambda current, runtime: pulse_socket_path(
            current.get("PULSE_SERVER"), runtime),
        values, "Pulse",
    )
    if error is not None:
        errors.append(error)
    elif path is None:
        errors.append("Pulse socket is unavailable")
    else:
        socket_error = _validate_socket_path(path, "Pulse")
        if socket_error is not None:
            errors.append(socket_error)

    # The session bus always uses a filesystem Unix path, never an abstract bus.
    path, error = _resource_path(
        lambda current, runtime: dbus_socket_path(
            current.get("DBUS_SESSION_BUS_ADDRESS"), runtime),
        values, "D-Bus",
    )
    if error is not None:
        errors.append(error)
    elif path is None:
        errors.append("D-Bus socket is unavailable")
    else:
        socket_error = _validate_socket_path(path, "D-Bus")
        if socket_error is not None:
            errors.append(socket_error)

    # Keep startup output useful without allowing environment-derived text to grow.
    return tuple(errors[:_MAX_ERRORS])


def main(env: Mapping[str, str] | None = None) -> int:
    """Print bounded preflight errors and return a stable status code."""

    # Validate all local daemon prerequisites before deciding the exit status.
    errors = validate_environment(env)

    # Keep successful startup silent so the launcher can hand over cleanly.
    if not errors:
        return 0

    # The messages above contain resource names only, never environment values.
    for error in errors:
        print(f"meeting-recorder preflight: {error[:_MAX_ERROR_LENGTH]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
