"""Zero-dependency tests for the bounded raw NetworkManager D-Bus adapter."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from typing import Any, Callable

from meeting_recorder.network_manager import (
    _GioBusTransport,
    NetworkManagerCancellation,
    NetworkManagerSSIDAdapter,
    NetworkSSIDResult,
    NetworkSSIDStatus,
)


NM_ROOT = "/org/freedesktop/NetworkManager"
NM_MANAGER = "org.freedesktop.NetworkManager"
ACTIVE_PATH = "/org/freedesktop/NetworkManager/ActiveConnection/1"
DEVICE_PATH = "/org/freedesktop/NetworkManager/Devices/1"
ACCESS_POINT_PATH = "/org/freedesktop/NetworkManager/AccessPoint/1"
NM_ACTIVE = "org.freedesktop.NetworkManager.Connection.Active"
NM_DEVICE = "org.freedesktop.NetworkManager.Device"
NM_WIRELESS = "org.freedesktop.NetworkManager.Device.Wireless"
NM_ACCESS_POINT = "org.freedesktop.NetworkManager.AccessPoint"


class _Variant:
    def __init__(self, signature: str, value: object) -> None:
        # Store the advertised type and opaque value for strict unpacking tests.
        self.signature = signature
        self.value = value

    def get_type_string(self) -> str:
        return self.signature

    def unpack(self) -> object:
        return self.value


class _Call:
    def __init__(
        self,
        destination: str,
        object_path: str,
        interface: str,
        method: str,
        parameter_signature: str | None,
        parameters: tuple[object, ...] | None,
        reply_signature: str,
        timeout_milliseconds: int,
    ) -> None:
        # Preserve every call field so the transport contract can be checked later.
        self.destination = destination
        self.object_path = object_path
        self.interface = interface
        self.method = method
        self.parameter_signature = parameter_signature
        self.parameters = parameters
        self.reply_signature = reply_signature
        self.timeout_milliseconds = timeout_milliseconds


class _Cancellable:
    def __init__(self) -> None:
        # Keep cancellation observable without importing Gio.
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _FakeGio:
    class Cancellable(_Cancellable):
        pass

    class DBusCallFlags:
        NONE = object()


class _FakeGLib:
    class Variant:
        def __init__(self, signature: str, value: object) -> None:
            # Preserve the constructed parameter variant for call-shape assertions.
            self.signature = signature
            self.value = value

    class VariantType:
        def __init__(self, signature: str) -> None:
            # Preserve the requested reply signature without loading GLib.
            self.signature = signature


class _FakeBus:
    def __init__(self) -> None:
        # Capture raw calls made through the Gio-shaped transport seam.
        self.calls: list[tuple[object, ...]] = []

    def call_sync(self, *args: object) -> object:
        # Preserve every positional argument from the official Gio call shape.
        self.calls.append(args)
        return "reply"


class _UnknownObjectError(Exception):
    pass


class _FakeTransport:
    def __init__(
        self,
        replies: dict[tuple[str, str], object],
        *,
        owner: object = _Variant("(b)", (True,)),
        failure: Exception | None = None,
        on_call: Callable[[_FakeTransport], None] | None = None,
    ) -> None:
        # Store typed replies and controlled failure hooks for one probe.
        self.replies = replies
        self.owner = owner
        self.failure = failure
        self.on_call = on_call
        self.calls: list[_Call] = []
        self.property_requests: list[tuple[str, str]] = []
        self.cancellable = _Cancellable()

    def new_cancellable(self) -> _Cancellable:
        # Return the same handle so cancellation can be observed by the test.
        return self.cancellable

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
        # Record the exact low-level call shape before serving a fake reply.
        self.calls.append(
            _Call(
                destination, object_path, interface, method,
                parameter_signature, parameters, reply_signature,
                timeout_milliseconds,
            )
        )

        # Let tests cancel or fail a call without exposing raw errors to the adapter.
        if self.on_call is not None:
            self.on_call(self)
        if self.failure is not None:
            raise self.failure

        # Match the NameHasOwner call separately from Properties.GetAll calls.
        if interface == "org.freedesktop.DBus" and method == "NameHasOwner":
            return self.owner

        # Return the typed property reply requested by the adapter.
        requested_interface = parameters[0] if parameters else None
        if not isinstance(requested_interface, str):
            raise AssertionError("fake GetAll call did not carry an interface")
        self.property_requests.append((object_path, requested_interface))
        return self.replies[(object_path, requested_interface)]


def _variant(signature: str, value: object) -> _Variant:
    return _Variant(signature, value)


def _properties(**values: tuple[str, object]) -> _Variant:
    # Wrap each fake property in the same typed variant shape as a{sv}.
    return _Variant(
        "(a{sv})",
        ({name: _variant(signature, value) for name, (signature, value) in values.items()},),
    )


def _fixture(
    *,
    ssids: tuple[bytes, ...] = (b"Office",),
    active_type: str = "802-11-wireless",
    active_state: int = 2,
    device_state: int | None = 100,
    access_point: str = ACCESS_POINT_PATH,
    device_path: str = DEVICE_PATH,
    active_path: str = ACTIVE_PATH,
) -> dict[tuple[str, str], object]:
    # Build the root, active connection, device, and wireless replies.
    replies: dict[tuple[str, str], object] = {
        (NM_ROOT, NM_MANAGER): _properties(
            ActiveConnections=("ao", (active_path,)),
        ),
        (active_path, NM_ACTIVE): _properties(
            Type=("s", active_type),
            State=("u", active_state),
            Devices=("ao", (device_path,)),
        ),
        (device_path, NM_DEVICE): _properties(
            DeviceType=("u", 2),
            ActiveConnection=("o", active_path),
            **({"State": ("u", device_state)} if device_state is not None else {}),
        ),
        (device_path, NM_WIRELESS): _properties(
            ActiveAccessPoint=("o", access_point),
        ),
    }
    # Add the access point reply unless the fixture intentionally uses '/'.
    if access_point != "/":
        replies[(access_point, NM_ACCESS_POINT)] = _properties(
            Ssid=("ay", ssids[0]),
        )
    return replies


def _adapter(
    transport: _FakeTransport,
    allowed: tuple[bytes, ...] = (b"Office",),
    **kwargs: Any,
) -> NetworkManagerSSIDAdapter:
    return NetworkManagerSSIDAdapter(
        allowed,
        bus_factory=lambda: transport,
        **kwargs,
    )


def test_import_does_not_load_gi() -> None:
    # Import the module in a subprocess that rejects every gi import.
    code = (
        "import builtins\n"
        "real = builtins.__import__\n"
        "def block(name, *args, **kwargs):\n"
        "    if name == 'gi' or name.startswith('gi.'):\n"
        "        raise AssertionError('gi imported at module scope')\n"
        "    return real(name, *args, **kwargs)\n"
        "builtins.__import__ = block\n"
        "import meeting_recorder.network_manager\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_gio_transport_uses_gio_cancellable_and_official_call_shape() -> None:
    # Keep Gio Cancellable separate from GLib Variant and VariantType helpers.
    bus = _FakeBus()
    transport = _GioBusTransport(bus, _FakeGio, _FakeGLib)
    cancellable = transport.new_cancellable()
    assert isinstance(cancellable, _FakeGio.Cancellable)

    # Match Gio.DBusConnection.call_sync(bus, path, interface, method, ...).
    reply = transport.call_sync(
        NM_MANAGER,
        NM_ROOT,
        "org.freedesktop.DBus.Properties",
        "GetAll",
        "(s)",
        (NM_MANAGER,),
        "(a{sv})",
        37,
        cancellable,
    )
    assert reply == "reply"
    assert len(bus.calls) == 1
    call = bus.calls[0]
    assert call[:4] == (
        NM_MANAGER, NM_ROOT, "org.freedesktop.DBus.Properties", "GetAll",
    )
    assert isinstance(call[4], _FakeGLib.Variant)
    assert isinstance(call[5], _FakeGLib.VariantType)
    assert call[6] is _FakeGio.DBusCallFlags.NONE
    assert call[7:] == (37, cancellable)


def test_allowed_disallowed_and_exact_raw_ssids() -> None:
    # Compare raw bytes without case folding, trimming, or decoding.
    cases = (
        (b"Office", (b"Office",), NetworkSSIDStatus.ALLOWED),
        (b"office", (b"Office",), NetworkSSIDStatus.DISALLOWED),
        (b"Office ", (b"Office",), NetworkSSIDStatus.DISALLOWED),
        (b"\xff\x00", (b"\xff\x00",), NetworkSSIDStatus.ALLOWED),
    )
    for current, allowed, expected in cases:
        # Resolve one deterministic SSID and compare the public decision.
        transport = _FakeTransport(_fixture(ssids=(current,)))
        result = _adapter(transport, allowed).probe()
        assert result.status is expected


def test_no_wifi_and_transition_states_are_unknown() -> None:
    # Non-Wi-Fi and transitional active connections cannot prove a stable SSID.
    cases = (
        lambda: _fixture(active_type="802-3-ethernet"),
        lambda: _fixture(active_state=1),
        lambda: _fixture(active_state=3),
    )
    for make_replies in cases:
        # Each response remains structurally valid but unsafe to classify.
        result = _adapter(_FakeTransport(make_replies())).probe()
        assert result == NetworkSSIDResult(NetworkSSIDStatus.UNKNOWN)


def test_multiple_identical_ssids_are_allowed_but_different_are_unknown() -> None:
    # Add a second validated device to exercise stable multi-device resolution.
    first = _fixture(ssids=(b"Office",))
    second_device = "/org/freedesktop/NetworkManager/Devices/2"
    second_ap = "/org/freedesktop/NetworkManager/AccessPoint/2"
    first[(NM_ROOT, NM_MANAGER)] = _properties(
        ActiveConnections=("ao", (ACTIVE_PATH,)),
    )
    first[(ACTIVE_PATH, NM_ACTIVE)] = _properties(
        Type=("s", "802-11-wireless"), State=("u", 2),
        Devices=("ao", (DEVICE_PATH, second_device)),
    )
    first[(second_device, NM_DEVICE)] = _properties(
        DeviceType=("u", 2), State=("u", 100), ActiveConnection=("o", ACTIVE_PATH),
    )
    first[(second_device, NM_WIRELESS)] = _properties(
        ActiveAccessPoint=("o", second_ap),
    )
    first[(second_ap, NM_ACCESS_POINT)] = _properties(Ssid=("ay", b"Office"))
    assert _adapter(_FakeTransport(first)).probe().status is NetworkSSIDStatus.ALLOWED

    # A disagreement is unknown even when one SSID is allowlisted.
    first[(second_ap, NM_ACCESS_POINT)] = _properties(Ssid=("ay", b"Other"))
    result = _adapter(_FakeTransport(first)).probe()
    assert result == NetworkSSIDResult(NetworkSSIDStatus.UNKNOWN)


def test_invalid_ssid_root_and_access_point_slash_are_unknown() -> None:
    # Empty or overlong bytes and the root object path mean no stable access point.
    empty = _adapter(_FakeTransport(_fixture(ssids=(b"",)))).probe()
    assert empty.status is NetworkSSIDStatus.UNKNOWN
    overlong = _adapter(_FakeTransport(_fixture(ssids=(b"x" * 33,)))).probe()
    assert overlong.status is NetworkSSIDStatus.UNKNOWN
    slash = _adapter(_FakeTransport(_fixture(access_point="/"))).probe()
    assert slash.status is NetworkSSIDStatus.UNKNOWN


def test_name_owner_false_and_bus_failures_are_unavailable() -> None:
    # Service absence and bus failures are unavailable, not policy decisions.
    owner = _FakeTransport(_fixture(), owner=_variant("(b)", (False,)))
    assert _adapter(owner).probe().status is NetworkSSIDStatus.UNAVAILABLE
    skipped_owner = _FakeTransport(_fixture())
    assert _adapter(skipped_owner, check_name_owner=False).probe().status is NetworkSSIDStatus.ALLOWED
    assert skipped_owner.calls[0].destination == "org.freedesktop.NetworkManager"
    failure = _FakeTransport(_fixture(), failure=PermissionError("private"))
    result = _adapter(failure).probe()
    assert result.status is NetworkSSIDStatus.UNAVAILABLE

    def failing_factory() -> _FakeTransport:
        raise OSError("private bus detail")

    adapter = NetworkManagerSSIDAdapter((b"Office",), bus_factory=failing_factory)
    assert adapter.probe().status is NetworkSSIDStatus.UNAVAILABLE


def test_timeout_and_cancellation_are_unavailable() -> None:
    # Low-level timeout errors map to unavailable without leaking their text.
    timeout = _FakeTransport(_fixture(), failure=TimeoutError("private timeout"))
    assert _adapter(timeout).probe().status is NetworkSSIDStatus.UNAVAILABLE

    cancellation = NetworkManagerCancellation()
    cancellation.cancel()
    # Pre-cancelled probes must not create a bus call.
    transport = _FakeTransport(_fixture())
    result = _adapter(transport).probe(cancellation)
    assert result.status is NetworkSSIDStatus.UNAVAILABLE
    assert not transport.calls

    during_call = NetworkManagerCancellation()
    # Cancellation during a call must reach the active fake Gio cancellable.
    active_transport = _FakeTransport(
        _fixture(), on_call=lambda _: during_call.cancel(),
    )
    result = _adapter(active_transport).probe(during_call)
    assert result.status is NetworkSSIDStatus.UNAVAILABLE
    assert active_transport.cancellable.cancelled


def test_unknown_object_and_malformed_variants_are_unknown() -> None:
    # Missing properties, wrong signatures, and vanished objects are unknown.
    missing_ssid = _fixture()
    missing_ssid[(ACCESS_POINT_PATH, NM_ACCESS_POINT)] = _properties()
    assert _adapter(_FakeTransport(missing_ssid)).probe().status is NetworkSSIDStatus.UNKNOWN

    wrong_type = _fixture()
    wrong_type[(ACTIVE_PATH, NM_ACTIVE)] = _properties(
        Type=("u", 2), State=("u", 2), Devices=("ao", (DEVICE_PATH,)),
    )
    assert _adapter(_FakeTransport(wrong_type)).probe().status is NetworkSSIDStatus.UNKNOWN

    unknown_ap = _fixture(access_point="/")
    assert _adapter(_FakeTransport(unknown_ap)).probe().status is NetworkSSIDStatus.UNKNOWN

    def disappear(transport: _FakeTransport) -> None:
        if len(transport.calls) == 2:
            raise _UnknownObjectError("private object detail")

    disappearing = _FakeTransport(_fixture(), on_call=disappear)
    assert _adapter(disappearing).probe().status is NetworkSSIDStatus.UNKNOWN


def test_active_and_device_limits_are_unknown() -> None:
    # Exceeding either response count bound is conservatively unknown.
    replies = _fixture()
    replies[(NM_ROOT, NM_MANAGER)] = _properties(
        ActiveConnections=("ao", (ACTIVE_PATH, "/org/freedesktop/NetworkManager/ActiveConnection/2")),
    )
    result = _adapter(_FakeTransport(replies), max_active_connections=1).probe()
    assert result.status is NetworkSSIDStatus.UNKNOWN

    replies = _fixture()
    replies[(ACTIVE_PATH, NM_ACTIVE)] = _properties(
        Type=("s", "802-11-wireless"), State=("u", 2),
        Devices=("ao", (DEVICE_PATH, "/org/freedesktop/NetworkManager/Devices/2")),
    )
    result = _adapter(_FakeTransport(replies), max_devices=1).probe()
    assert result.status is NetworkSSIDStatus.UNKNOWN


def test_overall_deadline_is_enforced() -> None:
    # Advance the injected monotonic clock past the deadline after one call.
    ticks = iter((0.0, 0.0, 0.0, 2.0))
    transport = _FakeTransport(_fixture())
    adapter = _adapter(
        transport, overall_timeout_seconds=1.0, monotonic=lambda: next(ticks),
    )
    assert adapter.probe().status is NetworkSSIDStatus.UNAVAILABLE
    assert len(transport.calls) == 1


def test_call_timeout_is_finite_and_active_connection_consistency_is_required() -> None:
    # Every call receives the finite configured timeout and known destination.
    transport = _FakeTransport(_fixture())
    result = _adapter(transport, call_timeout_milliseconds=37).probe()
    assert result.status is NetworkSSIDStatus.ALLOWED
    assert transport.calls
    assert all(call.timeout_milliseconds == 37 for call in transport.calls)
    assert all(call.destination == "org.freedesktop.NetworkManager" or call.destination == "org.freedesktop.DBus"
               for call in transport.calls)

    # The root call must request the manager interface on the manager path.
    root_call = next(call for call in transport.calls if call.object_path == NM_ROOT)
    assert root_call.interface == "org.freedesktop.DBus.Properties"
    assert root_call.parameters == (NM_MANAGER,)
    assert transport.property_requests == [
        (NM_ROOT, NM_MANAGER),
        (ACTIVE_PATH, NM_ACTIVE),
        (DEVICE_PATH, NM_DEVICE),
        (DEVICE_PATH, NM_WIRELESS),
        (ACCESS_POINT_PATH, NM_ACCESS_POINT),
    ]

    # A device attached to another active connection cannot be classified.
    inconsistent = _fixture()
    inconsistent[(DEVICE_PATH, NM_DEVICE)] = _properties(
        DeviceType=("u", 2), State=("u", 100),
        ActiveConnection=("o", "/org/freedesktop/NetworkManager/ActiveConnection/9"),
    )
    assert _adapter(_FakeTransport(inconsistent)).probe().status is NetworkSSIDStatus.UNKNOWN


__all__ = []
