"""Static checks for the container runtime assets and neutral systemd unit."""

import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
SYSTEMD_UNIT = ROOT / "systemd" / "meeting-recorder.service"
RUNTIME_ASSETS = (
    "meeting-recorder.desktop",
    "meeting-recorder-settings.desktop",
    "meeting-recorder.svg",
    "meeting-recorder-recording.svg",
    "meeting-recorder-paused.svg",
)
DEBIAN_ASSETS = (
    "build-deb.sh",
    "build-apt-repo.sh",
    "apt-index.html",
    "control",
    "copyright",
    "postinst",
    "prerm",
    "meeting-recorder.bin",
    "meeting-recorder.1",
    "meeting-recorder.service",
)


def test_runtime_assets_remain_available_to_the_container_build() -> None:
    # Keep the five runtime integration assets present and referenced by the image build.
    containerfile = (ROOT / "Containerfile").read_text(encoding="utf-8")
    for asset in RUNTIME_ASSETS:
        assert (PACKAGING / asset).is_file()
        assert f"COPY packaging/{asset} " in containerfile


def test_neutral_systemd_unit_preserves_cli_service_contract() -> None:
    # Keep the service unit outside packaging and retain the CLI/settings unit name.
    unit = SYSTEMD_UNIT.read_text(encoding="utf-8")
    cli = (ROOT / "meeting_recorder" / "__main__.py").read_text(encoding="utf-8")
    settings = (ROOT / "meeting_recorder" / "settings_gui.py").read_text(encoding="utf-8")

    # Require the unit to use the launcher installed in the user's local bin directory.
    assert "ExecStart=%h/.local/bin/meeting-recorder run" in unit
    assert "/usr/bin/meeting-recorder" not in unit
    assert "WantedBy=graphical-session.target" in unit
    assert '"meeting-recorder.service"' in cli
    assert '"meeting-recorder.service"' in settings


def test_debian_packaging_paths_and_exclusions_are_absent() -> None:
    # Reject restored Debian packaging files and stale Debian build-context exclusions.
    assert not (ROOT / "debian").exists()
    for asset in DEBIAN_ASSETS:
        assert not (PACKAGING / asset).exists()

    # Keep build context excludes focused on generated artifacts, not removed Debian paths.
    for ignore_file in (ROOT / ".containerignore", ROOT / "Containerfile.dockerignore"):
        text = ignore_file.read_text(encoding="utf-8")
        assert "debian/" not in text
        assert "*.deb" not in text
        assert "*.changes" not in text
        assert "*.buildinfo" not in text
        assert "*.dsc" not in text


def test_source_installer_owns_only_user_local_integration_paths() -> None:
    # Keep source activation bounded to user-local launcher, XDG links, and user systemd.
    installer = (ROOT / "scripts" / "install-source.sh").read_text(encoding="utf-8")

    assert "#!/usr/bin/env bash" in installer
    assert "--remove|--uninstall" in installer
    assert 'LAUNCHER="$HOME_DIR/.local/bin/meeting-recorder"' in installer
    assert 'UNIT_LINK="$CONFIG_HOME/systemd/user/meeting-recorder.service"' in installer
    assert 'DESKTOP_DIR="$DATA_HOME/applications"' in installer
    assert 'ICON_DIR="$DATA_HOME/icons/hicolor/scalable/apps"' in installer
    assert "systemctl --user daemon-reload" in installer
    assert "systemctl --user enable --now meeting-recorder.service" in installer
    assert "sudo" not in installer and "eval" not in installer


def test_source_installer_links_and_removes_only_its_user_files() -> None:
    # Use a temporary XDG home and systemctl shim so no host integration is changed.
    installer = ROOT / "scripts" / "install-source.sh"
    with tempfile.TemporaryDirectory() as temporary:
        temporary_root = Path(temporary)
        home = temporary_root / "home"
        commands = temporary_root / "commands"
        calls = temporary_root / "systemctl-calls"
        commands.mkdir()
        (commands / "systemctl").write_text(
            "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$SYSTEMCTL_CALLS\"\n",
            encoding="utf-8",
        )
        (commands / "systemctl").chmod(0o755)
        environment = {
            **os.environ,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_STATE_HOME": str(home / "state"),
            "SYSTEMCTL_CALLS": str(calls),
            "PATH": f"{commands}:{os.environ['PATH']}",
        }

        # Install with explicit activation and verify every generated path targets this checkout.
        subprocess.run([str(installer), "--enable"], cwd=ROOT, env=environment, check=True)
        launcher = home / ".local/bin/meeting-recorder"
        assert launcher.is_file()
        assert (home / "config/systemd/user/meeting-recorder.service").resolve() == SYSTEMD_UNIT
        assert (home / "data/applications/meeting-recorder.desktop").resolve() == PACKAGING / RUNTIME_ASSETS[0]
        assert "--user enable --now meeting-recorder.service" in calls.read_text(encoding="utf-8")

        # Run the generated launcher to prove it executes this checkout with system Python.
        version = subprocess.run(
            [str(launcher), "--version"], cwd=temporary_root, env=environment,
            check=True, capture_output=True, text=True,
        )
        assert version.stdout.strip() == "meeting-recorder 0.3.5"

        # Remove the owned integration and prove the launcher, unit, and state are gone.
        subprocess.run([str(installer), "--remove"], cwd=ROOT, env=environment, check=True)
        assert not (home / ".local/bin/meeting-recorder").exists()
        assert not (home / "config/systemd/user/meeting-recorder.service").exists()
        assert not (home / "state/meeting-recorder/source-install").exists()
