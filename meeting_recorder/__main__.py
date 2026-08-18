"""CLI entry point.

  meeting-recorder status     # service state + detected capture streams
  meeting-recorder start      # start the background service
  meeting-recorder stop       # stop it
  meeting-recorder restart    # restart it (after changing settings)
  meeting-recorder logs       # follow the service log
  meeting-recorder settings   # open the GTK settings window
  meeting-recorder run        # run the detector in the foreground (the service runs this)
  meeting-recorder record     # manual one-off recording (Ctrl-C to stop)
  meeting-recorder config     # create/print the user config file

The start/stop/restart/logs commands wrap `systemctl --user` so users never need
to remember that this is a *user* unit (it must be: it needs the caller's X
display, PulseAudio session and D-Bus session).
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys

from . import __version__
from .config import load_config, write_default_user_config
from .utils import LOG, build_output_path, setup_logging

_SERVICE = "meeting-recorder.service"
# How long `record` stays alive after saving, so the notification's Open Folder
# button still has a process to call back into. Bounded: the file is already
# safely written by this point, this is only about the button.
_NOTIFICATION_LINGER_SECONDS = 180


def _cmd_run(cfg) -> int:
    import gi
    gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    from .controller import Controller
    from .detector import MeetingDetector
    from .notifier import Notifier
    from .recorder import Recorder

    notifier = Notifier()
    recorder = Recorder(cfg)
    controller = Controller(cfg, notifier, recorder)
    detector = MeetingDetector(
        allowlist=cfg.allowlist,
        start_debounce=cfg.start_debounce_seconds,
        stop_debounce=cfg.stop_debounce_seconds,
        on_start=controller.on_meeting_start,
        on_stop=controller.on_meeting_stop,
    )

    loop = GLib.MainLoop()
    interval_ms = max(250, int(cfg.poll_interval_seconds * 1000))
    GLib.timeout_add(interval_ms, detector.tick)

    def _shutdown(*_a):
        LOG.info("Shutting down")
        controller.shutdown()
        loop.quit()
        return False

    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, _shutdown)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, _shutdown)

    LOG.info("Smart Meeting Recorder %s running (polling every %.1fs). "
             "Watching for: %s", __version__, cfg.poll_interval_seconds,
             ", ".join(sorted({e.app for e in cfg.allowlist})))
    loop.run()
    return 0


def _cmd_record(cfg) -> int:
    """Record immediately until Ctrl-C — useful for testing capture end-to-end.

    Driven by the same Controller the daemon uses, so a manual recording gets
    the tray icon, live timer and pause/resume controls (and, on Wayland, the
    ScreenCast handshake) instead of a second, poorer copy of that logic.
    """
    import gi
    gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    from .controller import Controller, State
    from .notifier import Notifier
    from .recorder import Recorder

    notifier = Notifier()
    recorder = Recorder(cfg)
    controller = Controller(cfg, notifier, recorder)
    loop = GLib.MainLoop()
    result: dict = {}

    def _on_finished(completed):
        result["completed"] = True
        result["path"] = completed.path if completed else None
        print(f"Saved: {completed.path}" if completed else "No file was saved.")
        # Do not quit yet. The "saved" notification's Open Folder button is a
        # callback into this process, so exiting now would leave a button that
        # silently does nothing. Wait for the user to act on (or dismiss) the
        # notification, with a cap so the command cannot hang forever.
        if completed is None or not notifier.has_live_notifications:
            loop.quit()
            return
        result["waiting"] = True
        GLib.timeout_add_seconds(1, _poll_notification)
        GLib.timeout_add_seconds(_NOTIFICATION_LINGER_SECONDS, _give_up)

    def _poll_notification():
        if notifier.has_live_notifications:
            return True                      # keep waiting
        loop.quit()
        return False

    def _give_up():
        loop.quit()
        return False

    # Fires however the recording ends: Ctrl-C, or Stop on the tray/pill.
    controller.on_finished = _on_finished

    # Portal cancellation has no detached finalization handle to complete.
    controller.on_manual_cancelled = loop.quit

    def _stop(*_a):
        # A second Ctrl-C, or one during the post-save wait for the
        # notification, means "just leave" — there is nothing left to finalize.
        if result.get("waiting") or result.get("completed"):
            loop.quit()
            return False
        # stop_manual() only *launches* finalize (concat + denoise + loudnorm);
        # the loop keeps running until on_finished reports the saved file,
        # otherwise exiting here would kill the finalize child and leave
        # orphaned .partN segments with no output.
        print("\nStopping — finalizing (denoise + loudness normalize)…")
        if not controller.stop_manual():
            loop.quit()
        return False

    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, _stop)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, _stop)

    controller.start_manual("Manual")
    if controller.state is not State.RECORDING:
        print("Could not start recording.")
        return 1
    print("Recording — press Ctrl-C here, or use the tray icon, to stop.")
    loop.run()

    return 0 if result.get("path") else 1


# -- service control (wraps `systemctl --user` so callers don't have to) ----

def _systemctl(*args: str) -> int:
    try:
        return subprocess.call(["systemctl", "--user", *args])
    except FileNotFoundError:
        print("systemctl not found — is systemd available?", file=sys.stderr)
        return 1


def _service_state() -> str:
    """'active', 'inactive', 'failed', … or 'not-installed'."""
    try:
        out = subprocess.run(["systemctl", "--user", "is-active", _SERVICE],
                             capture_output=True, text=True, timeout=5)
        state = out.stdout.strip()
        if state == "inactive":
            # Distinguish "installed but stopped" from "unit doesn't exist".
            shown = subprocess.run(
                ["systemctl", "--user", "show", "-p", "LoadState",
                 "--value", _SERVICE],
                capture_output=True, text=True, timeout=5).stdout.strip()
            if shown and shown != "loaded":
                return "not-installed"
        return state or "unknown"
    except (subprocess.SubprocessError, FileNotFoundError):
        return "unknown"


def _cmd_start(_cfg) -> int:
    return _systemctl("start", _SERVICE)


def _cmd_stop(_cfg) -> int:
    return _systemctl("stop", _SERVICE)


def _cmd_restart(_cfg) -> int:
    return _systemctl("restart", _SERVICE)


def _cmd_logs(_cfg) -> int:
    try:
        return subprocess.call(["journalctl", "--user", "-u", _SERVICE, "-f"])
    except FileNotFoundError:
        print("journalctl not found — is systemd available?", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


def _cmd_status(cfg) -> int:
    from .detector import match_meeting_app, query_source_outputs

    state = _service_state()
    mark = {"active": "●", "failed": "✗"}.get(state, "○")
    print(f"{mark} Service: {state}")
    if state == "not-installed":
        print("  (run from source? the background service isn't installed)")
    elif state != "active":
        print("  Start it with: meeting-recorder start")

    print("\nActive capture streams:")
    outputs = query_source_outputs()
    if not outputs:
        print("  none (or pactl unavailable)")
    for o in outputs:
        tag = " [monitor]" if o.is_monitor else ""
        print(f"  #{o.index} app={o.app_name!r} binary={o.binary!r} "
              f"source={o.source!r}{tag}")
    app = match_meeting_app(outputs, cfg.allowlist)
    print(f"\nMeeting match: {app or '(none)'}")
    return 0


def _cmd_config(_cfg) -> int:
    path = write_default_user_config()
    print(f"User config: {path}")
    return 0


def _cmd_settings(_cfg) -> int:
    """Open the GTK settings window."""
    from .settings_gui import run
    return run()


def _cmd_calendar(cfg, action: str) -> int:
    """Run the isolated Calendar credential command without starting recording."""
    from .calendar_oauth import (
        CalendarAuthorizationDeniedError, CalendarError, CalendarOAuth,
    )

    oauth = CalendarOAuth(cfg)
    try:
        if action == "connect":
            oauth.connect()
            print("Calendar: connected")
            return 0
        result = oauth.status() if action == "status" else oauth.disconnect()
    except CalendarAuthorizationDeniedError:
        print("Calendar: authorization cancelled or denied", file=sys.stderr)
        return 1
    except CalendarError as exc:
        print(f"Calendar: misconfigured ({exc})", file=sys.stderr)
        return 1
    detail = f" ({result.detail})" if result.detail else ""
    print(f"Calendar: {result.state}{detail}")
    return result.exit_code


def build_parser() -> argparse.ArgumentParser:
    """Build the command parser separately so the Calendar surface is testable."""
    # -v/--verbose accepted before OR after the subcommand via a shared parent.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true")

    parser = argparse.ArgumentParser(prog="meeting-recorder",
                                     description="Smart Meeting Recorder for Linux",
                                     parents=[common])
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")
    for name, help_text in (
        ("status", "service state + detected capture streams"),
        ("start", "start the background service"),
        ("stop", "stop the background service"),
        ("restart", "restart the service (apply setting changes)"),
        ("logs", "follow the service log"),
        ("settings", "open the settings window"),
        ("run", "run the detector in the foreground"),
        ("record", "record now until Ctrl-C"),
        ("config", "create/print the user config file"),
    ):
        sub.add_parser(name, parents=[common], help=help_text)
    calendar = sub.add_parser("calendar", parents=[common],
                              help="manage Google Calendar credentials")
    calendar_sub = calendar.add_subparsers(dest="calendar_command", required=True)
    for name, help_text in (
        ("connect", "authorize and securely store a Calendar refresh token"),
        ("status", "validate the stored Calendar credential"),
        ("disconnect", "revoke best-effort and remove local Calendar credentials"),
    ):
        calendar_sub.add_parser(name, parents=[common], help=help_text)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()

    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    cfg = load_config()

    command = args.command or "run"
    if command == "calendar":
        return _cmd_calendar(cfg, args.calendar_command)
    handler = {
        "run": _cmd_run,
        "record": _cmd_record,
        "status": _cmd_status,
        "config": _cmd_config,
        "settings": _cmd_settings,
        "start": _cmd_start,
        "stop": _cmd_stop,
        "restart": _cmd_restart,
        "logs": _cmd_logs,
    }[command]
    return handler(cfg)


if __name__ == "__main__":
    sys.exit(main())
