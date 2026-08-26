"""Static production policy checks for the Bluefin Quadlet deployment."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
UNIT = ROOT / "deploy" / "bluefin" / "meeting-recorder.container"
X11_DROP_IN = UNIT.with_name("meeting-recorder.container.d") / "50-x11.conf"


def _section_values(text: str, section: str, name: str) -> list[str]:
    """Return all values for one Quadlet directive without losing list entries."""

    values: list[str] = []
    active = False

    # Read only the requested section because Quadlet permits repeated directives.
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            active = line == f"[{section}]"
        elif active and line.startswith(f"{name}="):
            values.append(line.removeprefix(f"{name}="))

    return values


def test_base_unit_pins_the_image_and_confines_the_container() -> None:
    """Keep the published image immutable and the desktop container unprivileged."""

    # Read the complete production unit before checking its independent policies.
    text = UNIT.read_text(encoding="utf-8")

    # Require the published v0.4.1 image digest and an offline pull policy.
    assert _section_values(text, "Container", "Image") == [
        "ghcr.io/shadyf/meeting-recorder@sha256:f9514278412cb399f29dcbcaabd3d3be85e1b9b87dcf61cfbd32b026ffe83949"
    ]
    assert _section_values(text, "Container", "Pull") == ["never"]

    # Require the least-privilege runtime controls and journald-only container logs.
    for directive in (
        "ContainerName=meeting-recorder",
        "UserNS=keep-id",
        "SecurityLabelDisable=true",
        "ReadOnly=true",
        "ReadOnlyTmpfs=true",
        "NoNewPrivileges=true",
        "DropCapability=all",
        "LogDriver=journald",
    ):
        assert directive in text
    assert "trusted-desktop" in text.lower()

    # Reject privilege, device, and host-namespace escapes rather than assuming defaults.
    forbidden = (
        "Privileged=true",
        "AddDevice=",
        "Device=",
        "Network=host",
        "IPC=host",
        "--privileged",
        "--device",
        "/dev:/dev",
    )
    assert not any(value in text for value in forbidden)


def test_base_unit_exposes_only_approved_storage_and_desktop_resources() -> None:
    """Keep data writable while exposing only individual desktop socket files."""

    # Read the complete production unit before checking the mount allowlist.
    text = UNIT.read_text(encoding="utf-8")

    # Match the complete storage and live-resource allowlist, including mount modes.
    assert _section_values(text, "Container", "Volume") == [
        "%h/.config/meeting-recorder:/config/meeting-recorder:Z",
        "%h/.local/state/meeting-recorder:/state/meeting-recorder:Z",
        "%h/.cache/meeting-recorder:/cache/meeting-recorder:Z",
        "%h/Videos/MeetingRecorder:%h/Videos/MeetingRecorder:Z",
        "%t/wayland-0:/run/user/%U/wayland-0:ro",
        "%t/pulse/native:/run/user/%U/pulse/native:rw",
        "%t/bus:/run/user/%U/bus:rw",
        "/run/dbus/system_bus_socket:/run/dbus/system_bus_socket:ro",
    ]
    assert _section_values(text, "Container", "Tmpfs") == ["/tmp:rw,nosuid,nodev,noexec,size=64m"]

    # Forbid broad home, runtime, PipeWire, and socket relabel mounts.
    forbidden = ("%h:/", "%t:/run/user", "/run/user:/run/user", "pipewire-0", "/home:/home")
    assert not any(value in text for value in forbidden)


def test_base_unit_keeps_secret_oauth_and_session_settings_explicit() -> None:
    """Keep credentials out of unit text and make loopback OAuth reachable."""

    # Read the complete production unit before checking credentials and networking.
    text = UNIT.read_text(encoding="utf-8")

    # Mount the named secret with keep-id ownership and owner-read-only permissions.
    assert _section_values(text, "Container", "Secret") == [
        "meeting-recorder-speakr-token,target=/run/secrets/meeting-recorder-speakr-token,uid=%U,gid=%G,mode=0400"
    ]
    assert "MEETING_RECORDER_SPEAKR_TOKEN=" not in text

    # Allow one fixed OAuth listener through loopback only.
    assert _section_values(text, "Container", "PublishPort") == ["127.0.0.1:8765:8765/tcp"]
    expected_environment = {
        "TZ=Etc/UTC",
        "LANG=C.UTF-8",
        "XDG_CONFIG_HOME=/config",
        "XDG_STATE_HOME=/state",
        "XDG_CACHE_HOME=/cache",
        "XDG_RUNTIME_DIR=/run/user/%U",
        "XDG_SESSION_TYPE=wayland",
        "WAYLAND_DISPLAY=/run/user/%U/wayland-0",
        "PULSE_SERVER=unix:/run/user/%U/pulse/native",
        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%U/bus",
        "DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket",
        "MEETING_RECORDER_MANAGED_CONTAINER=1",
        "MEETING_RECORDER_GOOGLE_OAUTH_LISTEN_ADDRESS=0.0.0.0",
    }
    assert set(_section_values(text, "Container", "Environment")) == expected_environment


def test_base_unit_has_bounded_graphical_session_lifecycle() -> None:
    """Bind the service to the graphical user session without enabling lingering."""

    # Read the complete production unit before checking user-session lifecycle limits.
    text = UNIT.read_text(encoding="utf-8")

    # Tie both startup and shutdown to the graphical-session target.
    for directive in (
        "After=graphical-session.target",
        "PartOf=graphical-session.target",
        "WantedBy=graphical-session.target",
        "Restart=always",
        "RestartSec=10s",
    ):
        assert directive in text

    # Keep the application stop budget below systemd's outer service timeout.
    stop_timeout = _section_values(text, "Container", "StopTimeout")
    service_timeout = _section_values(text, "Service", "TimeoutStopSec")
    assert stop_timeout == ["20"]
    assert service_timeout == ["30s"]
    assert int(stop_timeout[0]) < int(service_timeout[0].removesuffix("s"))
    assert "Linger=" not in text


def test_x11_drop_in_replaces_wayland_lists_with_narrow_x11_access() -> None:
    """Ensure X11 is an alternative session profile, not an added host surface."""

    # Read the X11 profile and preserve every repeated mount directive for validation.
    text = X11_DROP_IN.read_text(encoding="utf-8")
    volumes = _section_values(text, "Container", "Volume")

    # An empty list assignment resets inherited socket mounts before replacements.
    assert volumes[0] == ""
    assert volumes[1:] == [
        "%h/.config/meeting-recorder:/config/meeting-recorder:Z",
        "%h/.local/state/meeting-recorder:/state/meeting-recorder:Z",
        "%h/.cache/meeting-recorder:/cache/meeting-recorder:Z",
        "%h/Videos/MeetingRecorder:%h/Videos/MeetingRecorder:Z",
        "%t/pulse/native:/run/user/%U/pulse/native:rw",
        "%t/bus:/run/user/%U/bus:rw",
        "/run/dbus/system_bus_socket:/run/dbus/system_bus_socket:ro",
        "/tmp/.X11-unix/X0:/tmp/.X11-unix/X0:ro",
        "%h/.Xauthority:%h/.Xauthority:ro",
    ]

    # Replace the session type and omit Wayland and broad X11 directory access.
    environment = _section_values(text, "Container", "Environment")
    assert environment == ["XDG_SESSION_TYPE=x11", "DISPLAY=:0", "XAUTHORITY=%h/.Xauthority", "WAYLAND_DISPLAY="]
    assert "wayland-0" not in text
    assert "/tmp/.X11-unix:/tmp/.X11-unix" not in text
    assert "xhost" not in text.lower()


def test_quadlet_generator_accepts_the_deployment_when_available() -> None:
    """Run Podman's Quadlet parser only on systems that provide it."""

    # Look up the optional generator because the zero-dependency runner has no skip support.
    generator = shutil.which("podman-system-generator")
    if generator is None:
        return

    # Let the installed generator parse the complete unit and drop-in directory.
    result = subprocess.run(
        [generator, "--user", "--dryrun", str(UNIT.parent)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


# Support direct focused execution without requiring the repository-wide test runner.
if __name__ == "__main__":
    # Run every top-level test function in the same way as tests/run_tests.py.
    for name in sorted(globals()):
        if name.startswith("test_"):
            globals()[name]()
