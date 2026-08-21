"""Bounded NetworkManager SSID checks through the raw Gio D-Bus API."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import threading
import time
from typing import Any, Callable, Protocol, runtime_checkable


_NM_SERVICE = "org.freedesktop.NetworkManager"
_NM_ROOT = "/org/freedesktop/NetworkManager"
_DBUS_SERVICE = "org.freedesktop.DBus"
_DBUS_ROOT = "/org/freedesktop/DBus"
_PROPERTIES = "org.freedesktop.DBus.Properties"
_NM_MANAGER = "org.freedesktop.NetworkManager"
_ACTIVE = "org.freedesktop.NetworkManager.Connection.Active"
_DEVICE = "org.freedesktop.NetworkManager.Device"
_WIRELESS = "org.freedesktop.NetworkManager.Device.Wireless"
_ACCESS_POINT = "org.freedesktop.NetworkManager.AccessPoint"
_GET_ALL = "GetAll"
_NAME_HAS_OWNER = "NameHasOwner"
_ACTIVE_STATE = 2
_DEVICE_WIFI = 2
_DEVICE_ACTIVATED = 100
_DEFAULT_TIMEOUT_SECONDS = 5.0
_DEFAULT_CALL_TIMEOUT_MILLISECONDS = 1000
_DEFAULT_MAX_ACTIVE_CONNECTIONS = 32
_DEFAULT_MAX_DEVICES = 64


class NetworkSSIDStatus(str, Enum):
    """The safe outcome of one synchronous SSID probe."""

    ALLOWED = "allowed"
    DISALLOWED = "disallowed"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class NetworkSSIDResult:
    """A public admission status without network identity or error details."""

    status: NetworkSSIDStatus

    def __post_init__(self) -> None:
        # Keep public result objects limited to the safe status enum.
        if not isinstance(self.status, NetworkSSIDStatus):
            raise ValueError("Network SSID status is invalid")


class NetworkManagerCancellation:
    """Thread-safe cancellation that can also cancel a Gio call."""

    def __init__(self) -> None:
        # Keep cancellation state separate from the optional Gio handle.
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._gio_cancellable: object | None = None

    @property
    def cancelled(self) -> bool:
        """Return whether the caller has requested cancellation."""
        return self._event.is_set()

    def cancel(self) -> None:
        """Request cancellation and safely forward it to the active Gio call."""
        # Publish the cancellation before reading the active call handle.
        self._event.set()

        # Read the handle under the same lock used when attaching it.
        with self._lock:
            cancellable = self._gio_cancellable

        # Forward cancellation outside the lock so Gio cannot block state access.
        if cancellable is not None:
            _cancel_gio_cancellable(cancellable)

    def _attach(self, cancellable: object | None) -> None:
        # Publish the active cancellable under the same lock used by cancel().
        with self._lock:
            self._gio_cancellable = cancellable
            already_cancelled = self._event.is_set()
        if already_cancelled and cancellable is not None:
            _cancel_gio_cancellable(cancellable)

    def _detach(self, cancellable: object | None) -> None:
        # Clear only the cancellable belonging to this completed probe.
        with self._lock:
            if self._gio_cancellable is cancellable:
                self._gio_cancellable = None


@runtime_checkable
class NetworkManagerCallTransport(Protocol):
    """Small injected boundary around one raw D-Bus ``call_sync`` operation."""

    def new_cancellable(self) -> object | None:
        ...

    def call_sync(
        self,
        destination: str,
        object_path: str,
        interface: str,
        method: str,
        parameter_signature: str | None,
        parameters: tuple[object, ...] | None,
        reply_signature: str,
        timeout_milliseconds: int,
        cancellable: object | None,
    ) -> object:
        ...


NetworkManagerBusFactory = Callable[[], NetworkManagerCallTransport]


class _MalformedReply(Exception):
    """Internal marker for a typed but unsafe D-Bus reply."""


class _ProbeUnavailable(Exception):
    """Internal marker for bus, service, timeout, or cancellation failure."""


class _ProbeLimitExceeded(_MalformedReply):
    """Internal marker for a response outside the configured safe bound."""


def _positive_number(value: object, name: str) -> float:
    """Validate a finite positive duration without retaining caller input."""
    # Reject non-numeric configuration before conversion.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be positive")

    # Reject overflow, non-finite, and non-positive durations.
    try:
        converted = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{name} must be positive") from None
    if not math.isfinite(converted) or converted <= 0:
        raise ValueError(f"{name} must be positive")
    return converted


def _positive_int(value: object, name: str) -> int:
    """Validate one positive integer bound."""
    # Keep every bound finite and strictly positive.
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _cancel_gio_cancellable(cancellable: object) -> None:
    """Cancel a Gio cancellable while hiding implementation-specific failures."""
    try:
        cancel = getattr(cancellable, "cancel")
        cancel()
    except Exception:
        pass


def _is_disappearing_object_error(error: Exception) -> bool:
    """Recognize only the safe D-Bus class for an object that vanished."""
    # Inspect stable error names without retaining or exposing the error text.
    names = [type(error).__name__]
    for attribute in ("get_dbus_name", "get_remote_error"):
        try:
            getter = getattr(error, attribute, None)
            if callable(getter):
                name = getter()
                if isinstance(name, str):
                    names.append(name)
        except Exception:
            pass
    return any(
        "unknownobject" in name.casefold() or "disappear" in name.casefold()
        for name in names
    )


def _variant_signature(value: object) -> str | None:
    """Read an optional testable or Gio variant signature."""
    try:
        getter = getattr(value, "get_type_string", None)
        if callable(getter):
            signature = getter()
            return signature if isinstance(signature, str) else None
        signature = getattr(value, "signature", None)
        return signature if isinstance(signature, str) else None
    except Exception:
        raise _MalformedReply from None


def _unpack_typed(value: object, expected_signature: str) -> object:
    """Unpack one optional variant and enforce its advertised D-Bus type."""
    # Validate a signature when the injected transport exposes one.
    signature = _variant_signature(value)
    if signature is not None and signature != expected_signature:
        raise _MalformedReply

    # Unpack Gio and test variants without importing either implementation.
    unpack = getattr(value, "unpack", None)
    if callable(unpack):
        try:
            return unpack()
        except Exception:
            raise _MalformedReply from None
    return value


def _properties(reply: object, expected_interface: str) -> dict[str, object]:
    """Extract one strictly shaped ``Properties.GetAll`` result."""
    # The raw GetAll reply is a one-tuple containing an a{sv} dictionary.
    unpacked = _unpack_typed(reply, "(a{sv})")
    if type(unpacked) is not tuple or len(unpacked) != 1:
        raise _MalformedReply
    values = unpacked[0]
    if type(values) is not dict:
        raise _MalformedReply
    if any(type(key) is not str for key in values):
        raise _MalformedReply
    # The interface argument documents which contract the caller requested.
    if not expected_interface:
        raise _MalformedReply
    return values


def _property(
    values: dict[str, object], name: str, signature: str,
) -> object:
    """Read one required typed property from a validated property map."""
    if name not in values:
        raise _MalformedReply
    return _unpack_typed(values[name], signature)


def _optional_property(
    values: dict[str, object], name: str, signature: str,
) -> object | None:
    """Read one optional typed property, preserving absence as ``None``."""
    if name not in values:
        return None
    return _unpack_typed(values[name], signature)


def _object_path(value: object) -> str:
    """Validate one D-Bus object path without interpreting its target."""
    # Reject malformed paths before using them in another authenticated call.
    if (
        type(value) is not str
        or not value.startswith("/")
        or "//" in value
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in value)
    ):
        raise _MalformedReply
    return value


def _object_paths(value: object, limit: int) -> tuple[str, ...]:
    """Validate one bounded ``ao`` value and preserve its order."""
    unpacked = _unpack_typed(value, "ao")
    if not isinstance(unpacked, (tuple, list)):
        raise _MalformedReply
    if len(unpacked) > limit:
        raise _ProbeLimitExceeded
    return tuple(_object_path(item) for item in unpacked)


def _byte_array(value: object) -> bytes:
    """Return a bounded raw ``ay`` value without exposing its contents."""
    unpacked = _unpack_typed(value, "ay")
    if isinstance(unpacked, bytes):
        result = unpacked
    elif isinstance(unpacked, (bytearray, memoryview)):
        result = bytes(unpacked)
    elif isinstance(unpacked, (tuple, list)) and all(
        type(item) is int and 0 <= item <= 255 for item in unpacked
    ):
        result = bytes(unpacked)
    else:
        raise _MalformedReply

    # Reject empty or overlong SSIDs before policy matching.
    if not result or len(result) > 32:
        raise _MalformedReply
    return result


class _GioBusTransport:
    """Concrete raw Gio wrapper used only after lazy loading succeeds."""

    def __init__(self, bus: Any, gio: Any, glib: Any) -> None:
        self._bus = bus
        self._gio = gio
        self._glib = glib

    def new_cancellable(self) -> object:
        # Gio owns Cancellable; GLib is used only for Variant construction.
        return self._gio.Cancellable()

    def call_sync(
        self,
        destination: str,
        object_path: str,
        interface: str,
        method: str,
        parameter_signature: str | None,
        parameters: tuple[object, ...] | None,
        reply_signature: str,
        timeout_milliseconds: int,
        cancellable: object | None,
    ) -> object:
        # Construct variants here so tests can inject plain typed values.
        parameter_variant = None
        if parameter_signature is not None and parameters is not None:
            parameter_variant = self._glib.Variant(parameter_signature, parameters)
        reply_type = self._glib.VariantType(reply_signature)
        return self._bus.call_sync(
            destination,
            object_path,
            interface,
            method,
            parameter_variant,
            reply_type,
            self._gio.DBusCallFlags.NONE,
            timeout_milliseconds,
            cancellable,
        )


def _gio_bus_factory() -> NetworkManagerCallTransport:
    """Lazy-load Gio and open the system bus in the caller's worker thread."""
    try:
        import gi  # type: ignore

        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gio, GLib  # type: ignore

        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        return _GioBusTransport(bus, Gio, GLib)
    except Exception:
        raise _ProbeUnavailable from None


class NetworkManagerSSIDAdapter:
    """Perform one bounded NetworkManager SSID probe over raw Gio D-Bus."""

    def __init__(
        self,
        allowed_ssids: tuple[bytes, ...] | frozenset[bytes],
        *,
        bus_factory: NetworkManagerBusFactory | None = None,
        overall_timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        call_timeout_milliseconds: int = _DEFAULT_CALL_TIMEOUT_MILLISECONDS,
        max_active_connections: int = _DEFAULT_MAX_ACTIVE_CONNECTIONS,
        max_devices: int = _DEFAULT_MAX_DEVICES,
        check_name_owner: bool = True,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        # Accept only exact opaque bytes for the caller's allowlist.
        if not isinstance(allowed_ssids, (tuple, frozenset)):
            raise ValueError("allowed SSIDs must be a tuple or frozenset")
        if any(type(ssid) is not bytes for ssid in allowed_ssids):
            raise ValueError("allowed SSIDs must contain bytes")
        if type(check_name_owner) is not bool:
            raise ValueError("NameHasOwner setting is invalid")
        if not callable(monotonic):
            raise ValueError("monotonic clock is invalid")

        # Store validated policy and bounded operation settings.
        self.allowed_ssids = frozenset(allowed_ssids)
        self.overall_timeout_seconds = _positive_number(
            overall_timeout_seconds, "overall timeout",
        )
        self.call_timeout_milliseconds = _positive_int(
            call_timeout_milliseconds, "call timeout",
        )
        self.max_active_connections = _positive_int(
            max_active_connections, "active connection limit",
        )
        self.max_devices = _positive_int(max_devices, "device limit")
        self.check_name_owner = check_name_owner
        self._bus_factory = bus_factory or _gio_bus_factory
        self._monotonic = monotonic

    def probe(
        self, cancellation: NetworkManagerCancellation | None = None,
    ) -> NetworkSSIDResult:
        """Run one synchronous probe and return only a safe typed result."""
        if cancellation is None:
            cancellation = NetworkManagerCancellation()
        if not isinstance(cancellation, NetworkManagerCancellation):
            raise ValueError("Network cancellation is invalid")

        # Stop before creating a bus when the caller already cancelled.
        if cancellation.cancelled:
            return NetworkSSIDResult(NetworkSSIDStatus.UNAVAILABLE)

        try:
            return self._probe(cancellation)
        except _MalformedReply:
            return NetworkSSIDResult(NetworkSSIDStatus.UNKNOWN)
        except _ProbeUnavailable:
            return NetworkSSIDResult(NetworkSSIDStatus.UNAVAILABLE)
        except Exception:
            return NetworkSSIDResult(NetworkSSIDStatus.UNAVAILABLE)

    def _probe(self, cancellation: NetworkManagerCancellation) -> NetworkSSIDResult:
        # Establish the transport and one operation-wide monotonic deadline.
        deadline = self._monotonic() + self.overall_timeout_seconds
        if deadline <= self._monotonic():
            raise _ProbeUnavailable
        try:
            transport = self._bus_factory()
        except _ProbeUnavailable:
            raise
        except Exception:
            raise _ProbeUnavailable from None
        if not isinstance(transport, NetworkManagerCallTransport):
            raise _ProbeUnavailable
        if cancellation.cancelled:
            raise _ProbeUnavailable

        # Bind one thread-safe Gio cancellable to the whole probe.
        gio_cancellable = None
        try:
            gio_cancellable = transport.new_cancellable()
        except Exception:
            raise _ProbeUnavailable from None
        cancellation._attach(gio_cancellable)
        try:
            # Optionally confirm that NetworkManager owns its well-known name.
            if self.check_name_owner:
                owner = self._call(
                    transport, cancellation, deadline,
                    _DBUS_SERVICE, _DBUS_ROOT, _DBUS_SERVICE,
                    _NAME_HAS_OWNER, "(s)", (_NM_SERVICE,), "(b)",
                )
                owner_values = _unpack_typed(owner, "(b)")
                if (
                    not isinstance(owner_values, tuple)
                    or len(owner_values) != 1
                    or type(owner_values[0]) is not bool
                ):
                    raise _MalformedReply
                if not owner_values[0]:
                    raise _ProbeUnavailable

            # Read the root's active connection paths under a strict bound.
            root = self._call_properties(
                transport, cancellation, deadline, _NM_ROOT, _NM_MANAGER,
            )
            active_paths = _object_paths(
                _property(root, "ActiveConnections", "ao"),
                self.max_active_connections,
            )
            if not active_paths:
                raise _MalformedReply

            # Resolve only activated Wi-Fi active connections and their devices.
            device_paths: list[tuple[str, str]] = []

            # Read each active connection before deciding whether it is Wi-Fi.
            for active_path in active_paths:
                if active_path == "/":
                    raise _MalformedReply
                active = self._call_properties(
                    transport, cancellation, deadline, active_path, _ACTIVE,
                )
                connection_type = _property(active, "Type", "s")
                state = _property(active, "State", "u")
                if type(connection_type) is not str or type(state) is not int:
                    raise _MalformedReply
                if connection_type != "802-11-wireless":
                    continue
                if state != _ACTIVE_STATE:
                    raise _MalformedReply
                devices = _object_paths(
                    _property(active, "Devices", "ao"), self.max_devices,
                )
                if not devices:
                    raise _MalformedReply
                if len(device_paths) + len(devices) > self.max_devices:
                    raise _ProbeLimitExceeded
                device_paths.extend((device, active_path) for device in devices)

            # No activated Wi-Fi connection is safe to classify as a network.
            if not device_paths:
                raise _MalformedReply

            # Resolve each Wi-Fi device's active access point without scanning.
            resolved_ssids: list[bytes] = []

            # Read only the active access point attached to each validated device.
            for device_path, active_path in device_paths:
                if device_path == "/":
                    raise _MalformedReply
                device = self._call_properties(
                    transport, cancellation, deadline, device_path, _DEVICE,
                )
                if _property(device, "DeviceType", "u") != _DEVICE_WIFI:
                    raise _MalformedReply
                device_state = _optional_property(device, "State", "u")
                if device_state is not None and device_state != _DEVICE_ACTIVATED:
                    raise _MalformedReply
                if _property(device, "ActiveConnection", "o") != active_path:
                    raise _MalformedReply
                wireless = self._call_properties(
                    transport, cancellation, deadline, device_path, _WIRELESS,
                )
                access_point = _property(wireless, "ActiveAccessPoint", "o")
                access_point_path = _object_path(access_point)
                if access_point_path == "/":
                    raise _MalformedReply
                access_point_values = self._call_properties(
                    transport, cancellation, deadline,
                    access_point_path, _ACCESS_POINT,
                )
                ssid = _byte_array(_property(access_point_values, "Ssid", "ay"))
                resolved_ssids.append(ssid)

            # Require all resolved Wi-Fi devices to report one stable SSID.
            stable_ssid = resolved_ssids[0]
            if any(ssid != stable_ssid for ssid in resolved_ssids[1:]):
                raise _MalformedReply
            status = (
                NetworkSSIDStatus.ALLOWED
                if stable_ssid in self.allowed_ssids
                else NetworkSSIDStatus.DISALLOWED
            )
            return NetworkSSIDResult(status)
        finally:
            cancellation._detach(gio_cancellable)

    def _call_properties(
        self,
        transport: NetworkManagerCallTransport,
        cancellation: NetworkManagerCancellation,
        deadline: float,
        object_path: str,
        interface: str,
    ) -> dict[str, object]:
        """Read one bounded Properties.GetAll dictionary."""
        reply = self._call(
            transport, cancellation, deadline,
            _NM_SERVICE, object_path, _PROPERTIES, _GET_ALL,
            "(s)", (interface,), "(a{sv})",
        )
        return _properties(reply, interface)

    def _call(
        self,
        transport: NetworkManagerCallTransport,
        cancellation: NetworkManagerCancellation,
        deadline: float,
        destination: str,
        object_path: str,
        interface: str,
        method: str,
        parameter_signature: str | None,
        parameters: tuple[object, ...] | None,
        reply_signature: str,
    ) -> object:
        """Make one finite raw call while enforcing cancellation and deadline."""
        # Calculate a finite timeout from the remaining operation budget.
        if cancellation.cancelled:
            raise _ProbeUnavailable
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise _ProbeUnavailable
        timeout = min(
            self.call_timeout_milliseconds,
            max(1, math.ceil(remaining * 1000)),
        )

        # Convert all low-level call failures into the safe unavailable result.
        try:
            reply = transport.call_sync(
                destination,
                object_path,
                interface,
                method,
                parameter_signature,
                parameters,
                reply_signature,
                timeout,
                cancellation._gio_cancellable,
            )
        except Exception as error:
            if _is_disappearing_object_error(error):
                raise _MalformedReply from None
            raise _ProbeUnavailable from None
        if cancellation.cancelled or self._monotonic() >= deadline:
            raise _ProbeUnavailable
        return reply


__all__ = [
    "NetworkManagerBusFactory",
    "NetworkManagerCallTransport",
    "NetworkManagerCancellation",
    "NetworkManagerSSIDAdapter",
    "NetworkSSIDResult",
    "NetworkSSIDStatus",
]
