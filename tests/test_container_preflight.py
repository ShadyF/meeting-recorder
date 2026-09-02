"""Zero-dependency tests for the container resource admission check."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from container import preflight


def _unix_socket(directory: Path, name: str) -> tuple[socket.socket, Path]:
    """Create and listen on a temporary filesystem Unix socket."""

    # Bind a real listening socket so preflight only needs filesystem metadata.
    path = directory / name
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    # Keep the socket alive until the test has completed its validation.
    listener.bind(str(path))
    listener.listen(1)

    return listener, path


def _valid_environment(directory: Path) -> tuple[dict[str, str], list[socket.socket]]:
    """Build a valid environment and retain each listening socket."""

    # Create each required daemon resource as a filesystem Unix socket.
    wayland, wayland_path = _unix_socket(directory, "wayland-0")
    pulse, pulse_path = _unix_socket(directory, "pulse-native")
    bus, bus_path = _unix_socket(directory, "bus")

    # Use explicit session addresses so fallback behavior can be tested separately.
    environment = {
        "TZ": "Etc/UTC",
        "XDG_SESSION_TYPE": "wayland",
        "WAYLAND_DISPLAY": str(wayland_path),
        "PULSE_SERVER": f"unix:{pulse_path}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={bus_path},guid=redacted-test-guid",
        "XDG_RUNTIME_DIR": str(directory),
    }
    return environment, [wayland, pulse, bus]


def test_all_valid_resources() -> None:
    """Accept valid timezone data and listening Unix sockets."""

    # Keep required socket listeners alive while the valid context is checked.
    with tempfile.TemporaryDirectory() as raw_directory:
        environment, listeners = _valid_environment(Path(raw_directory))
        try:
            # Validate all required resources while their listeners remain open.
            assert preflight.validate_environment(environment) == ()
        finally:
            # Release every socket after the metadata-only check completes.
            for listener in listeners:
                listener.close()


def test_missing_and_wrong_type_resources() -> None:
    """Reject missing sockets and regular files without opening services."""

    # Build a valid context before replacing each resource with an invalid path.
    with tempfile.TemporaryDirectory() as raw_directory:
        directory = Path(raw_directory)
        environment, listeners = _valid_environment(directory)
        try:
            # Replace each socket with a missing or regular path before checking.
            environment["WAYLAND_DISPLAY"] = str(directory / "missing-wayland")
            environment["PULSE_SERVER"] = f"unix:{directory / 'wrong-pulse'}"
            (directory / "wrong-pulse").write_text("not a socket", encoding="utf-8")
            environment["DBUS_SESSION_BUS_ADDRESS"] = (
                f"unix:path={directory / 'missing-bus'},guid=hidden"
            )
            errors = preflight.validate_environment(environment)

            # Keep the three independent resource failures bounded and named.
            assert len(errors) == 3
            assert "Wayland" in errors[0]
            assert "Pulse" in errors[1]
            assert "D-Bus" in errors[2]
        finally:
            # Release listeners even when an assertion reports a bad error set.
            for listener in listeners:
                listener.close()


def test_all_required_variables_absent() -> None:
    """Reject an environment that provides none of the four daemon resources."""
    # Assert the stable bounded error set for every missing required resource.
    errors = preflight.validate_environment({})
    assert errors == (
        "TZ is missing",
        "desktop session type is unsupported",
        "Pulse socket is unavailable",
        "D-Bus socket is unavailable",
    )


def test_each_required_resource_fails_independently() -> None:
    """Keep each required resource independently fail-closed."""

    # Keep all listeners available while removing one resource per iteration.
    with tempfile.TemporaryDirectory() as raw_directory:
        environment, listeners = _valid_environment(Path(raw_directory))
        try:
            # Exercise each socket variable independently from the valid baseline.
            for variable, label in (
                ("WAYLAND_DISPLAY", "Wayland"),
                ("PULSE_SERVER", "Pulse"),
                ("DBUS_SESSION_BUS_ADDRESS", "D-Bus"),
            ):
                # Remove one input at a time while preserving the other resources.
                missing = environment.copy()
                missing.pop(variable)
                if variable == "DBUS_SESSION_BUS_ADDRESS":
                    # Keep the explicit Pulse and Wayland resources valid while
                    # making only the D-Bus runtime fallback unavailable.
                    missing["XDG_RUNTIME_DIR"] = str(Path(raw_directory) / "empty-runtime")
                errors = preflight.validate_environment(missing)
                assert len(errors) == 1
                assert label in errors[0]

            missing_timezone = environment.copy()
            missing_timezone.pop("TZ")

            # Timezone failure remains independent from the socket checks.
            assert preflight.validate_environment(missing_timezone) == ("TZ is missing",)
        finally:
            for listener in listeners:
                listener.close()


def test_timezone_validation() -> None:
    """Reject missing, malformed, and unknown Region/City timezones."""
    # Cover each timezone admission branch without reading environment state.
    assert preflight.validate_timezone(None) == "TZ is missing"
    assert preflight.validate_timezone("UTC") == "TZ is malformed"
    assert preflight.validate_timezone("Not/A_Real_Zone") == "TZ is unknown"
    assert preflight.validate_timezone("Europe/Paris") is None


def test_wayland_absolute_and_relative_forms() -> None:
    """Resolve both supported Wayland display forms."""
    runtime = "/run/user/1000"

    # Absolute display paths do not depend on XDG_RUNTIME_DIR.
    assert preflight.wayland_socket_path("/tmp/wayland-0", runtime) == Path("/tmp/wayland-0")

    # Relative display names resolve below the absolute runtime directory.
    assert preflight.wayland_socket_path("wayland-0", runtime) == Path(
        "/run/user/1000/wayland-0"
    )

    # Relative names without a usable runtime root must fail closed.
    try:
        preflight.wayland_socket_path("wayland-0", None)
    except ValueError:
        pass
    else:
        raise AssertionError("relative Wayland display must require runtime directory")


def test_pulse_server_parsing_and_fallback() -> None:
    """Parse Unix Pulse addresses and the runtime fallback."""
    runtime = "/run/user/1000"

    # Explicit absolute and runtime-relative Unix addresses are both supported.
    assert preflight.pulse_socket_path("unix:/tmp/pulse", runtime) == Path("/tmp/pulse")
    assert preflight.pulse_socket_path("unix:pulse/native", runtime) == Path(
        "/run/user/1000/pulse/native"
    )
    assert preflight.pulse_socket_path(None, runtime) == Path("/run/user/1000/pulse/native")

    # Reject non-Unix addresses before any socket path can be considered.
    try:
        preflight.pulse_socket_path("tcp:127.0.0.1", runtime)
    except ValueError:
        pass
    else:
        raise AssertionError("non-Unix Pulse address must be rejected")


def test_dbus_parsing_and_fallback() -> None:
    """Parse comma-separated D-Bus fields and the runtime fallback."""
    runtime = "/run/user/1000"
    address = "unix:path=/tmp/session-bus,guid=secret-value;tcp:host=bad"

    # Parse the filesystem path while ignoring opaque GUID and transport fields.
    assert preflight.dbus_socket_path(address, runtime) == Path("/tmp/session-bus")
    assert preflight.dbus_socket_path(None, runtime) == Path("/run/user/1000/bus")

    # Abstract bus addresses are not filesystem sockets and must be rejected.
    try:
        preflight.dbus_socket_path("unix:abstract=not-a-filesystem-socket", runtime)
    except ValueError:
        pass
    else:
        raise AssertionError("abstract D-Bus address must be rejected")


def test_optional_system_and_credential_variables_can_be_absent() -> None:
    """Accept required resources without system bus or credential variables."""

    # Validate required resources while unrelated service credentials stay absent.
    with tempfile.TemporaryDirectory() as raw_directory:
        environment, listeners = _valid_environment(Path(raw_directory))
        try:
            # Keep unrelated desktop and credential resources out of the test environment.
            optional_variables = (
                "DBUS_SYSTEM_BUS_ADDRESS",
                "SECRET_SERVICE",
                "SECRET_SERVICE_BUS_ADDRESS",
                "MEETING_RECORDER_TOKEN",
                "OAUTH_TOKEN",
                "OAUTH_CLIENT_SECRET",
            )
            assert all(variable not in environment for variable in optional_variables)
            assert preflight.validate_environment(environment) == ()
        finally:
            # Release all required-resource listeners after validation.
            for listener in listeners:
                listener.close()


def test_errors_are_bounded_and_redacted() -> None:
    """Keep invalid startup output short and free of environment values."""
    secret = "super-secret-session-value"

    # Supply hostile values and verify that only safe bounded classifications escape.
    environment = {
        "TZ": secret,
        "XDG_SESSION_TYPE": secret,
        "WAYLAND_DISPLAY": secret,
        "PULSE_SERVER": f"tcp:{secret}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={secret},guid={secret}",
    }
    errors = preflight.validate_environment(environment)
    assert errors
    assert len(errors) <= 4
    assert all(len(error) <= 128 for error in errors)
    assert all(secret not in error for error in errors)


def test_x11_requires_exact_socket_mapping_and_nonempty_authority_file() -> None:
    """Accept only a local X11 socket and an available authority file."""
    with tempfile.TemporaryDirectory() as raw_directory:
        # Keep the X11 socket root and every other test resource private to this test.
        directory = Path(raw_directory)
        environment, listeners = _valid_environment(directory)
        xauthority = directory / "Xauthority"
        xauthority.write_bytes(b"cookie")
        x11_directory = directory / "x11-sockets"
        x11_directory.mkdir()
        x11, _x11_path = _unix_socket(x11_directory, "X88")
        try:
            # Exercise the production display mapping against the isolated socket root.
            environment.update({
                "XDG_SESSION_TYPE": "x11",
                "DISPLAY": ":88",
                "XAUTHORITY": str(xauthority),
            })
            assert preflight.validate_environment(
                environment, x11_socket_root=x11_directory) == ()

            # Reject remote display forms even when the local socket exists.
            environment["DISPLAY"] = "localhost:88"
            assert "X11 display is malformed" in preflight.validate_environment(
                environment, x11_socket_root=x11_directory)

            # Reject empty authority files after the exact socket mapping succeeds.
            environment["DISPLAY"] = ":88"
            xauthority.write_bytes(b"")
            assert "Xauthority file is unavailable" in preflight.validate_environment(
                environment, x11_socket_root=x11_directory)
        finally:
            # Close the isolated socket before TemporaryDirectory removes its path.
            x11.close()
            for listener in listeners:
                listener.close()


def test_cli_returns_stable_nonzero_for_invalid_resources() -> None:
    """Return the documented nonzero status without printing secret values."""
    source = Path(__file__).parents[1] / "container" / "preflight.py"
    environment = {"TZ": "bad-value"}

    # Execute the actual preflight entrypoint without requiring session resources.
    result = subprocess.run(
        [sys.executable, str(source)],
        env={**os.environ, **environment},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "bad-value" not in result.stderr


def test_launcher_bypasses_preflight_for_other_commands() -> None:
    """Allow version and other non-daemon commands without session sockets."""
    launcher = Path(__file__).parents[1] / "container" / "meeting-recorder"

    # The production launcher must bypass preflight for a version request.
    result = subprocess.run(
        [str(launcher), "--version"],
        cwd=Path(__file__).parents[1],
        env={**os.environ, "TZ": "bad-value"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "meeting-recorder 0.4.4" in result.stdout


def _controlled_launcher_invocation(arguments: list[str]) -> tuple[bool, list[str]]:
    """Run a copied launcher with safe stand-ins for its fixed image paths."""
    launcher_source = Path(__file__).parents[1] / "container" / "meeting-recorder"

    # Isolate each dispatch run and its marker files in a temporary directory.
    with tempfile.TemporaryDirectory() as raw_directory:
        directory = Path(raw_directory)
        fake_python = directory / "python3"
        fake_preflight = directory / "preflight.py"
        controlled_launcher = directory / "meeting-recorder"
        preflight_log = directory / "preflight.log"
        module_log = directory / "module.log"

        # Make preflight leave only a marker, without connecting to any service.
        fake_preflight.write_text(
            "#!/bin/sh\nprintf '%s\\n' preflight >> \"$PREFLIGHT_LOG\"\n",
            encoding="utf-8",
        )
        fake_preflight.chmod(0o755)

        # Make the Python stand-in execute preflight or record module arguments.
        fake_python.write_text(
            "#!/bin/sh\n"
            "if [ \"${2:-}\" = \"$CONTROLLED_PREFLIGHT\" ]; then\n"
            "    \"$2\"\n"
            "    exit $?\n"
            "fi\n"
            ": > \"$MODULE_LOG\"\n"
            "for argument do printf '%s\\n' \"$argument\" >> \"$MODULE_LOG\"; done\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)

        # Substitute only in this temporary copy; production paths stay fixed.
        controlled_source = launcher_source.read_text(encoding="utf-8")
        controlled_source = controlled_source.replace(
            "/usr/bin/python3", str(fake_python),
        ).replace(
            "/opt/meeting-recorder/preflight.py", str(fake_preflight),
        )
        controlled_launcher.write_text(controlled_source, encoding="utf-8")
        controlled_launcher.chmod(0o755)

        # Preserve every argument while recording whether preflight ran first.
        result = subprocess.run(
            [str(controlled_launcher), *arguments],
            env={
                **os.environ,
                "CONTROLLED_PREFLIGHT": str(fake_preflight),
                "PREFLIGHT_LOG": str(preflight_log),
                "MODULE_LOG": str(module_log),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0

        # Read bounded local markers instead of exposing any environment values.
        preflight_ran = preflight_log.exists() and preflight_log.read_text() == "preflight\n"
        module_arguments = module_log.read_text().splitlines()
        return preflight_ran, module_arguments


def test_launcher_dispatches_daemon_argument_orderings() -> None:
    """Run preflight for default, run, and supported verbosity orderings."""
    cases = (
        ([], ["run"]),
        (["-v"], ["-v"]),
        (["--verbose"], ["--verbose"]),
        (["run"], ["run"]),
        (["run", "-v"], ["run", "-v"]),
        (["run", "--verbose"], ["run", "--verbose"]),
        (["-v", "run"], ["-v", "run"]),
        (["--verbose", "run"], ["--verbose", "run"]),
        (["-v", "-v", "run"], ["-v", "-v", "run"]),
        (["--verbose", "--verbose", "run"], ["--verbose", "--verbose", "run"]),
    )

    # Check each ordering through the controlled image-path equivalent.
    for arguments, expected_arguments in cases:
        preflight_ran, module_arguments = _controlled_launcher_invocation(arguments)
        assert preflight_ran
        assert module_arguments == ["-B", "-m", "meeting_recorder", *expected_arguments]


def test_launcher_bypasses_preflight_for_admin_orderings() -> None:
    """Bypass preflight for administrative, help, and version commands."""
    cases = (
        ["settings"],
        ["-v", "settings"],
        ["--verbose", "status"],
        ["--help"],
        ["-v", "--help"],
        ["--verbose", "--version"],
        ["run", "--help"],
    )

    # Verify administrative paths preserve arguments without running preflight.
    for arguments in cases:
        preflight_ran, module_arguments = _controlled_launcher_invocation(arguments)
        assert not preflight_ran
        assert module_arguments == ["-B", "-m", "meeting_recorder", *arguments]
