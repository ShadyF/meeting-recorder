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
import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from . import __version__
from .config import (
    Config, PublicationMode, load_config, require_speakr_token, resolve_speakr_url,
    write_default_user_config,
)
from .domain import CompletedRecording
from .speakr_domain import PublicationJob, PublicationResult
from .utils import LOG, build_output_path, setup_logging

if TYPE_CHECKING:
    from .speakr_publisher import SpeakrPublisher

_SERVICE = "meeting-recorder.service"
# How long `record` stays alive after saving, so the notification's Open Folder
# button still has a process to call back into. Bounded: the file is already
# safely written by this point, this is only about the button.
_NOTIFICATION_LINGER_SECONDS = 180
_SPEAKR_WAIT_CODE = 3
_SPEAKR_DUE_LIMIT = 100


def _publication_rename_tracker(service: Any | None = None) -> Callable[[Path, Path], None]:
    """Build a synchronous CLI tracker or a worker-backed daemon tracker."""
    if service is not None:
        return _daemon_publication_rename_tracker(service)

    # The blocking correction command keeps this durable update synchronous.
    holder: dict[str, Any] = {}

    def track(old_path: Path, new_path: Path) -> None:
        try:
            from .speakr_domain import MediaIdentity
            from .speakr_store import PublicationStore, default_database_path

            old_absolute = Path(os.path.abspath(os.fspath(old_path)))
            new_absolute = Path(os.path.abspath(os.fspath(new_path)))
            info = os.lstat(new_absolute)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                return
            identity = MediaIdentity(
                new_absolute, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            )

            # Do not create publication state just because an untracked file was renamed.
            store = holder.get("store")
            if store is None:
                database_path = default_database_path()
                if not database_path.exists():
                    return
                store = PublicationStore(database_path)
                holder["store"] = store
            store.update_path(
                os.fsencode(old_absolute), os.fsencode(new_absolute), identity,
            )
        except Exception as exc:
            # Publication tracking must never affect a committed recording move.
            LOG.debug("Publication rename tracking unavailable: %s", type(exc).__name__)

    return track


def _daemon_publication_rename_tracker(service: Any) -> Callable[[Path, Path], None]:
    """Capture post-move identity on GLib and submit all store work to the worker."""

    def track(old_path: Path, new_path: Path) -> None:
        try:
            from .speakr_domain import MediaIdentity

            old_absolute = Path(os.path.abspath(os.fspath(old_path)))
            new_absolute = Path(os.path.abspath(os.fspath(new_path)))
            info = os.lstat(new_absolute)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                return
            identity = MediaIdentity(
                new_absolute, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            )
            service.submit_rename(old_absolute, new_absolute, identity)
        except Exception:
            # Rename tracking must never affect a committed recording move.
            LOG.debug("Publication rename tracking unavailable")

    return track


def _cmd_run(cfg) -> int:
    import gi
    gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    from .controller import Controller
    from .detector import MeetingDetector
    from .notifier import Notifier
    from .recorder import Recorder
    from .calendar_cache import CalendarCache
    from .calendar_google import GoogleCalendarClient
    from .calendar_oauth import CalendarOAuth
    from .calendar_refresh import CalendarRefresher
    from .calendar_service import CalendarRefreshService
    from .config import load_raw_config, validate_google_calendar_ids
    from .recording_enrichment import RecordingEnricher, cache_only_occurrence_provider

    notifier = Notifier()
    publication_service = None
    local_notice_pending = False

    def _safe_notice_value(value: object, fallback: str = "unknown") -> str:
        # Keep notification fields to short identifiers even if an injected seam misbehaves.
        rendered = value if isinstance(value, str) else fallback
        if not rendered or len(rendered) > 64 or any(
            not (char.isascii() and (char.isalnum() or char in "._-"))
            for char in rendered
        ):
            return fallback
        return rendered

    def _show_publication_notice(
        action: str, job_id: str | None, error_code: str | None,
    ) -> None:
        # Marshal each service transition notice independently; the service owns deduplication.
        safe_action = _safe_notice_value(action, "publication")
        safe_job = _safe_notice_value(job_id, "") if job_id is not None else None
        safe_error = _safe_notice_value(error_code, "unknown") if error_code else "unknown"

        def show() -> bool:
            # Notifier is GTK-backed, so this callback is deliberately GLib-owned.
            try:
                job_text = safe_job or "none"
                notifier.info(
                    "Speakr publication action required",
                    f"action={safe_action} job={job_text} error={safe_error}",
                    persistent=True,
                )
            except Exception:
                LOG.warning("Could not show publication notice")
            return False

        try:
            GLib.idle_add(show)
        except Exception:
            LOG.warning("Could not schedule publication notice")

    def _show_local_publication_notice(error_code: str) -> None:
        # Bound repeated queue failures without suppressing versioned worker notices.
        nonlocal local_notice_pending
        if local_notice_pending:
            return
        local_notice_pending = True

        def show_local() -> bool:
            nonlocal local_notice_pending
            local_notice_pending = False
            _show_publication_notice("publication", None, error_code)
            return False

        try:
            GLib.idle_add(show_local)
        except Exception:
            local_notice_pending = False
            LOG.warning("Could not schedule publication notice")

    def _publication_notice_callback(notice: Any) -> None:
        # Worker notices contain only the safe action, job ID, and error code.
        _show_publication_notice(notice.action, notice.job_id, notice.error_code)

    publication_service_class: Any | None = None
    try:
        from .speakr_service import PublicationService as publication_service_class
    except Exception:
        LOG.warning("Speakr publication service is unavailable")

    # Optional publication setup must never prevent the recorder from starting.
    # Config is always complete in production; the attribute check keeps older test
    # and recovery configurations fail-closed without constructing D-Bus state.
    if publication_service_class is not None and hasattr(cfg, "speakr_publication_mode"):
        try:
            publication_service = publication_service_class(
                cfg, notice_callback=_publication_notice_callback,
            )
            publication_service.start()
        except Exception:
            LOG.warning("Speakr publication service is unavailable")
            if publication_service is not None:
                try:
                    publication_service.stop(1)
                except Exception:
                    LOG.warning("Speakr publication service cleanup failed")
            publication_service = None

    recorder = Recorder(cfg)
    enricher = RecordingEnricher(
        cache_only_occurrence_provider(),
        on_media_renamed=(
            _publication_rename_tracker(publication_service)
            if publication_service is not None else None
        ),
    ).enrich
    controller = Controller(cfg, notifier, recorder, recording_enricher=enricher)

    def _on_finished(completed: CompletedRecording | None) -> None:
        # Completion callbacks only perform immutable queue admission on GLib.
        if (completed is None or not isinstance(completed, CompletedRecording)
                or publication_service is None):
            return
        try:
            accepted = publication_service.submit_completed(completed)
        except Exception:
            _show_local_publication_notice("submission_failed")
            return
        try:
            mode = PublicationMode.parse(getattr(cfg, "speakr_publication_mode", "disabled"))
        except Exception:
            mode = PublicationMode.DISABLED
        if not accepted and mode is PublicationMode.AUTOMATIC:
            _show_local_publication_notice("queue_full")

    controller.on_finished = _on_finished
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

    # Keep optional Calendar I/O off the GLib thread and reload selection each cycle.
    def _selected_calendar_ids():
        try:
            return validate_google_calendar_ids(load_raw_config().get("google_calendar_ids", []))
        except ValueError:
            return ()

    calendar_service = None
    try:
        # Calendar is optional, so setup failures must not block meeting detection.
        calendar_service = CalendarRefreshService(
            CalendarRefresher(GoogleCalendarClient(CalendarOAuth(cfg).access_token), CalendarCache()),
            _selected_calendar_ids)
        calendar_service.start()
    except Exception:
        LOG.warning("Calendar background refresh is unavailable", exc_info=True)

    def _shutdown(*_a):
        loop.quit()
        return False

    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, _shutdown)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, _shutdown)

    LOG.info("Smart Meeting Recorder %s running (polling every %.1fs). "
             "Watching for: %s", __version__, cfg.poll_interval_seconds,
             ", ".join(sorted({e.app for e in cfg.allowlist})))
    try:
        loop.run()
    finally:
        LOG.info("Shutting down")
        # Stop optional Calendar work without skipping recorder finalization on failure.
        if calendar_service is not None:
            try:
                if not calendar_service.stop(1):
                    LOG.warning("Calendar background refresh did not stop before shutdown timeout")
            except Exception:
                LOG.warning("Calendar background refresh cleanup failed", exc_info=True)
        try:
            controller.shutdown()
        except Exception:
            LOG.warning("Recorder controller cleanup failed", exc_info=True)
        if publication_service is not None:
            try:
                if not publication_service.stop(2):
                    LOG.warning("Speakr publication service did not stop before shutdown timeout")
            except Exception:
                LOG.warning("Speakr publication service cleanup failed")
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
    from .recording_enrichment import RecordingEnricher, cache_only_occurrence_provider

    notifier = Notifier()
    recorder = Recorder(cfg)
    enricher = RecordingEnricher(
        cache_only_occurrence_provider(),
        # The one-shot command enqueues the final path after GLib exits; it does
        # not need to touch publication state while enriching the sidecar.
        on_media_renamed=None,
    ).enrich
    controller = Controller(cfg, notifier, recorder, recording_enricher=enricher)
    loop = GLib.MainLoop()
    result: dict[str, object] = {}

    def _on_finished(completed: CompletedRecording | None) -> None:
        # Preserve the immutable completion object until the GLib loop has ended.
        result["completed"] = completed
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

    # Finish any detached finalization while the controller still owns its handles.
    try:
        controller.shutdown()
    except Exception:
        LOG.warning("Recorder controller cleanup failed", exc_info=True)

    completed = result.get("completed")
    if not isinstance(completed, CompletedRecording):
        return 1

    # Automatic one-shot publication is deliberately outside GLib and token-free.
    try:
        mode = PublicationMode.parse(getattr(cfg, "speakr_publication_mode", "disabled"))
    except Exception:
        mode = PublicationMode.DISABLED
    if mode is PublicationMode.AUTOMATIC:
        try:
            origin = resolve_speakr_url(cfg)
            publisher = _speakr_publisher()
            publisher.enqueue(completed.path, origin)
        except Exception:
            print(
                "WARNING: Speakr publication could not be queued; recording was saved.",
                file=sys.stderr,
            )

    return 0


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


def _speakr_status(job: PublicationJob) -> dict[str, object]:
    """Return only bounded operational fields safe for terminal output."""
    return {
        "job_id": job.job_id,
        "state": job.state.value,
        "action": job.operation,
        "resume_intent": job.resume_intent,
        "origin": job.key.instance_url,
        "sha256": job.key.recording_sha256,
        "attempts": job.attempt_count,
        "next_attempt_at_ms": job.next_attempt_at_ms,
        "remote_recording_id": job.remote_recording_id,
        "last_error_code": job.last_error_code,
        "last_http_status": job.last_http_status,
    }


def _print_speakr_status(job: PublicationJob) -> None:
    print(json.dumps(_speakr_status(job), sort_keys=True))


def _speakr_origin(cfg: Config) -> str | None:
    """Resolve the HTTPS origin without reading the bearer token."""
    try:
        return resolve_speakr_url(cfg)
    except (TypeError, ValueError):
        print("Speakr: invalid instance URL configuration.", file=sys.stderr)
        return None


def _speakr_token() -> str | None:
    """Read the bearer token only after local admission and SSID checks."""
    try:
        return require_speakr_token()
    except (TypeError, ValueError):
        print("Speakr: bearer token is missing or invalid.", file=sys.stderr)
        return None


def _speakr_network_allowed(cfg: Config, force: bool) -> bool:
    """Apply the one-shot SSID admission gate without exposing network identity."""
    if force:
        return True

    # Missing and empty allowlists fail closed before constructing D-Bus state.
    allowed = getattr(cfg, "speakr_allowed_ssid_bytes", None)
    if not allowed:
        print("Speakr: waiting for an allowed network.", file=sys.stderr)
        return False
    from .network_manager import NetworkManagerSSIDAdapter, NetworkSSIDStatus

    try:
        result = NetworkManagerSSIDAdapter(allowed).probe()
        status = NetworkSSIDStatus(getattr(result, "status", result))
    except Exception:
        status = NetworkSSIDStatus.UNAVAILABLE
    if status is NetworkSSIDStatus.ALLOWED:
        return True
    print("Speakr: waiting for an allowed network.", file=sys.stderr)
    return False


def _speakr_publisher() -> SpeakrPublisher:
    from .speakr_http import StdlibSpeakrTransport
    from .speakr_publisher import SpeakrPublisher
    from .speakr_store import PublicationStore

    return SpeakrPublisher(PublicationStore(), StdlibSpeakrTransport())


def _speakr_result_code(result: PublicationResult) -> int:
    from .speakr_domain import PublicationState

    _print_speakr_status(result.job)
    if result.job.state is PublicationState.PUBLISHED:
        print("Speakr: already published." if result.already_published else "Speakr: published.")
        return 0
    print(
        f"Speakr: action required; publication remains {result.job.state.value}.",
        file=sys.stderr,
    )
    return 1


def _cmd_speakr_upload(
    cfg: Config,
    path: str | None = None,
    *,
    all_jobs: bool = False,
    status_job: str | None = None,
    status_all: bool = False,
    retry_job: str | None = None,
    relink_job: str | None = None,
    relink_path: str | None = None,
    forget_job: str | None = None,
    force: bool = False,
) -> int:
    """Run one explicit Speakr operation without exposing private values."""
    from .speakr_domain import PublicationState

    operation_count = sum(item is not None for item in (
        status_job, retry_job, relink_job, forget_job,
    ))
    if status_all and status_job is not None:
        print("Speakr: --status JOB cannot be combined with --status --all.", file=sys.stderr)
        return 2
    if status_all and not all_jobs:
        print("Speakr: --status --all requires the --all form.", file=sys.stderr)
        return 2
    if operation_count > 1 or (operation_count and (all_jobs or path is not None)):
        print("Speakr: Speakr upload options are ambiguous.", file=sys.stderr)
        return 2
    if status_all and operation_count:
        print("Speakr: Speakr upload options are ambiguous.", file=sys.stderr)
        return 2
    if force and (status_all or status_job is not None or relink_job is not None
                  or forget_job is not None):
        print("Speakr: --force is allowed only with PATH, --all, or --retry JOB.",
              file=sys.stderr)
        return 2
    if not any((path is not None, all_jobs, status_all, operation_count)):
        print("Speakr: upload requires PATH, --all, or one operation option.", file=sys.stderr)
        return 2
    if all_jobs and path is not None:
        print("Speakr: PATH cannot be combined with --all.", file=sys.stderr)
        return 2

    try:
        publisher = _speakr_publisher()
    except Exception:
        print("Speakr: publication state is unavailable.", file=sys.stderr)
        return 1

    # Local inspection and mutations intentionally do not resolve credentials.
    if status_job is not None or status_all:
        if status_all:
            for job in publisher.list():
                _print_speakr_status(job)
            return 0
        assert status_job is not None
        status_result = publisher.get(status_job)
        if status_result is None:
            print("Speakr: publication job was not found.", file=sys.stderr)
            return 2
        _print_speakr_status(status_result)
        return 0
    if relink_job is not None:
        try:
            relinked_job = publisher.relink(relink_job, relink_path or "")
        except Exception:
            print("Speakr: relink failed; the local job was not changed.", file=sys.stderr)
            return 1
        _print_speakr_status(relinked_job)
        return 0
    if forget_job is not None:
        try:
            if publisher.get(forget_job) is None:
                print("Speakr: publication job was not found.", file=sys.stderr)
                return 2
            publisher.forget(forget_job)
        except Exception:
            print("Speakr: forget failed; the local job was not changed.", file=sys.stderr)
            return 1
        print(f"Speakr: forgot {forget_job}.")
        return 0

    try:
        # Resolve origin before any token lookup, then keep all local validation local.
        instance_url = _speakr_origin(cfg)
        if instance_url is None:
            return 2
        if retry_job is not None:
            retry_result = publisher.get(retry_job)
            if retry_result is None:
                print("Speakr: publication job was not found.", file=sys.stderr)
                return 2
            if retry_result.key.instance_url != instance_url:
                print("Speakr: publication job origin does not match configuration.", file=sys.stderr)
                return 2
            if (retry_result.state is PublicationState.UNCERTAIN
                    and not retry_result.reconciliation_eligible):
                print(
                    "WARNING: explicit retry may create a duplicate Speakr recording.",
                    file=sys.stderr,
                )
            if not _speakr_network_allowed(cfg, force):
                return _SPEAKR_WAIT_CODE
            token = _speakr_token()
            if token is None:
                # Let the engine claim and block the current phase before any reset.
                blocked = publisher.block_configuration(retry_job, instance_url=instance_url)
                if blocked is None:
                    print("Speakr: publication job is leased or not due.", file=sys.stderr)
                    return 1
                _print_speakr_status(blocked)
                return 1
            reset = publisher.retry(retry_job)
            result = publisher.run_one(instance_url, token, reset.job_id)
            if result is None:
                print("Speakr: publication job is leased or not due.", file=sys.stderr)
                return 1
            return _speakr_result_code(result)
        if all_jobs:
            # Inspect a bounded local due snapshot before touching D-Bus or credentials.
            due_job_ids = getattr(publisher, "due_job_ids", None)
            if callable(due_job_ids):
                if not due_job_ids(instance_url, limit=_SPEAKR_DUE_LIMIT):
                    print("Speakr: no due publication jobs.")
                    return 0
            if not _speakr_network_allowed(cfg, force):
                return _SPEAKR_WAIT_CODE
            token = _speakr_token()
            # The engine snapshots due IDs again to prevent zero-delay starvation.
            results = publisher.run_all_due(instance_url, token or "")
            if not results:
                print("Speakr: no due publication jobs.")
                return 0
            result_codes = [_speakr_result_code(result) for result in results]
            return 0 if all(code == 0 for code in result_codes) else 1
        if path is None:
            print("Speakr: PATH is required.", file=sys.stderr)
            return 2
        # PATH form persists or reuses the local job before network admission.
        job = publisher.enqueue(path, instance_url)
        if not _speakr_network_allowed(cfg, force):
            return _SPEAKR_WAIT_CODE
        token = _speakr_token()
        if token is None:
            # Let the engine claim and block the queued phase before any transfer attempt.
            blocked = publisher.block_configuration(job.job_id, instance_url=instance_url)
            if blocked is None:
                print("Speakr: publication job is leased or not due.", file=sys.stderr)
                return 1
            _print_speakr_status(blocked)
            return 1
        result = publisher.run_one(instance_url, token or "", job.job_id)
        if result is None:
            print("Speakr: publication job is leased or not due.", file=sys.stderr)
            return 1
        return _speakr_result_code(result)
    except Exception:
        print("Speakr: publication failed; no private error details are available.", file=sys.stderr)
        return 1


def _cmd_settings(_cfg) -> int:
    """Open the GTK settings window."""
    from .settings_gui import run
    return run()


def _correction_refresh(cfg) -> None:
    """Perform only an explicit blocking refresh; correction remains cache-only otherwise."""
    from .calendar_cache import CalendarCache
    from .calendar_google import GoogleCalendarClient
    from .calendar_oauth import CalendarOAuth
    from .calendar_refresh import CalendarRefresher
    from .config import load_raw_config, validate_google_calendar_ids

    try:
        selected = validate_google_calendar_ids(load_raw_config().get("google_calendar_ids", []))
        if not selected:
            return
        report = CalendarRefresher(
            GoogleCalendarClient(CalendarOAuth(cfg).access_token), CalendarCache()).refresh(
                selected, blocking=True)
        if not report.success:
            print("Calendar correction: refresh failed; using cached data.", file=sys.stderr)
    except Exception as exc:
        print(f"Calendar correction: refresh unavailable ({type(exc).__name__}); using cached data.",
              file=sys.stderr)


def _cmd_calendar_correct(cfg, recording: str, refresh: bool,
                          selector: str | None, clear: bool) -> int:
    from .calendar_domain import decode_occurrence_selector, meeting_snapshot
    from .recording_enrichment import (
        CorrectionTransactionError,
        RecordingCorrectionService,
        cache_only_occurrence_provider,
    )

    def report_failure(error: CorrectionTransactionError) -> int:
        print(json.dumps({
            "error": "calendar_correction_failed",
            "current_path": str(error.outcome.current_path),
            "partial": error.outcome.partial,
            "committed": error.outcome.committed,
            "error_code": error.outcome.error_code,
        }, sort_keys=True), file=sys.stderr)
        return 1

    if clear:
        service = RecordingCorrectionService((), on_media_renamed=_publication_rename_tracker())
        try:
            before = service.discover(Path(recording))
            final = service.clear(Path(recording))
            if service.discover(final) is not None:
                raise OSError("clear did not remove recording metadata")
            print(f"Recording: {'already clear' if before is None else final}")
            return 0
        except CorrectionTransactionError as exc:
            return report_failure(exc)
        except (OSError, ValueError):
            print("Calendar correction failed (invalid recording metadata).", file=sys.stderr)
            return 1
    if refresh:
        _correction_refresh(cfg)
    provider = cache_only_occurrence_provider()
    occurrences = tuple(provider())
    service = RecordingCorrectionService(
        occurrences, on_media_renamed=_publication_rename_tracker(),
    )
    try:
        if selector is None:
            if service.discover(Path(recording)) is None:
                raise ValueError("recording sidecar is missing")
            rows = service.list_nearby(Path(recording), occurrences)
            for row in rows:
                snapshot = meeting_snapshot(row.occurrence)
                title = snapshot.title if snapshot.details_visible else "<private>"
                local = row.occurrence.start_utc.astimezone()
                print(json.dumps({
                    "selector": row.selector,
                    "current": row.is_current,
                    "title": title,
                    "scheduled_utc": row.occurrence.start_utc.isoformat().replace("+00:00", "Z"),
                    "scheduled_local": local.isoformat(),
                }, ensure_ascii=False, sort_keys=True))
            return 0
        selected_key = decode_occurrence_selector(selector)
        final = service.select(Path(recording), selected_key, occurrences)
        metadata = service.discover(final)
        if (metadata is None or metadata.meeting is None
                or metadata.meeting.occurrence_key != selected_key):
            raise OSError("selection was not committed")
        print(f"Recording: {final}")
        return 0
    except CorrectionTransactionError as exc:
        return report_failure(exc)
    except (OSError, ValueError):
        print("Calendar correction failed (invalid recording metadata).", file=sys.stderr)
        return 1


def _cmd_calendar(cfg, action: str, ids: list[str] | None = None, clear: bool = False,
                  recording: str | None = None, refresh: bool = False,
                  selector: str | None = None) -> int:
    """Run the isolated Calendar credential command without starting recording."""
    if action == "correct":
        assert recording is not None
        return _cmd_calendar_correct(cfg, recording, refresh, selector, clear)
    from .calendar_oauth import (
        CalendarAuthorizationDeniedError, CalendarError, CalendarOAuth,
    )
    from .calendar_cache import CalendarCache
    from .calendar_google import CalendarApiError, GoogleCalendarClient
    from .calendar_refresh import CalendarRefresher
    from .config import load_raw_config, save_google_calendar_ids, validate_google_calendar_ids

    oauth = CalendarOAuth(cfg)
    try:
        if action == "select":
            if clear:
                save_google_calendar_ids([])
                print("Calendar: selection cleared")
                return 0
            requested = validate_google_calendar_ids(ids or [])
            if not requested:
                raise CalendarError("select requires at least one --id or --clear")
            available = {item.id for item in GoogleCalendarClient(oauth.access_token).list_calendars()}
            if any(item not in available for item in requested):
                raise CalendarError("one or more selected calendars are inaccessible")
            save_google_calendar_ids(list(requested))
            print("Calendar: selection saved")
            return 0
        if action == "list":
            selected = validate_google_calendar_ids(load_raw_config().get("google_calendar_ids", []))
            for item in GoogleCalendarClient(oauth.access_token).list_calendars():
                marker = " selected" if item.id in selected else ""
                print(f"{json.dumps(item.id)} {json.dumps(item.summary)}{marker}")
            return 0
        if action == "refresh":
            selected = validate_google_calendar_ids(load_raw_config().get("google_calendar_ids", []))
            if not selected:
                print("Calendar: refresh complete (no calendars selected)")
                return 0
            report = CalendarRefresher(GoogleCalendarClient(oauth.access_token), CalendarCache()).refresh(
                selected, blocking=True)
            for result in report.results:
                print(f"Calendar: {json.dumps(result.calendar_id)} {'ok' if result.success else result.detail}")
            return 0 if report.success else 1
        if action == "connect":
            oauth.connect()
            print("Calendar: connected")
            return 0
        result = oauth.status() if action == "status" else oauth.disconnect()
    except CalendarAuthorizationDeniedError:
        print("Calendar: authorization cancelled or denied", file=sys.stderr)
        return 1
    except (CalendarError, CalendarApiError, OSError, ValueError) as exc:
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
    calendar_sub.add_parser("list", parents=[common], help="list accessible calendars")
    select = calendar_sub.add_parser("select", parents=[common], help="select accessible calendars")
    select_group = select.add_mutually_exclusive_group(required=True)
    select_group.add_argument("--id", dest="calendar_ids", action="append", default=[])
    select_group.add_argument("--clear", action="store_true")
    calendar_sub.add_parser("refresh", parents=[common], help="refresh selected Calendar caches")
    correct = calendar_sub.add_parser("correct", parents=[common],
                                      help="correct one recording from cached Calendar data")
    correct.add_argument("recording")
    correct.add_argument("--refresh", action="store_true",
                         help="refresh Calendar caches before reading them")
    correct_group = correct.add_mutually_exclusive_group()
    correct_group.add_argument("--select", dest="selector")
    correct_group.add_argument("--clear", action="store_true")
    speakr = sub.add_parser("speakr", parents=[common], help="explicitly publish recordings to Speakr")
    speakr_sub = speakr.add_subparsers(dest="speakr_command", required=True)
    upload = speakr_sub.add_parser("upload", parents=[common], help="publish or inspect Speakr recordings")
    upload.add_argument("path", nargs="?", help="recording path for an explicit upload")
    upload.add_argument("--all", dest="all_jobs", action="store_true",
                        help="run all due jobs; combine with --status for --status --all")
    upload.add_argument("--force", action="store_true",
                        help="skip the allowed-SSID check for this explicit operation")
    upload.add_argument("--status", nargs="?", const="", default=None, metavar="JOB",
                        help="print one job status, or use --status --all")
    action_group = upload.add_mutually_exclusive_group()
    action_group.add_argument("--retry", dest="retry_job", metavar="JOB",
                              help="explicitly authorize and retry one job")
    action_group.add_argument("--relink", dest="relink_args", nargs=2,
                              metavar=("JOB", "NEW_PATH"), help="securely relink one job")
    action_group.add_argument("--forget", dest="forget_job", metavar="JOB",
                              help="forget one local job")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()

    args = parser.parse_args(argv)
    if args.command == "speakr" and args.speakr_command == "upload":
        if args.status == "" and not args.all_jobs:
            parser.error("speakr upload --status requires --all when JOB is omitted")
        if args.status not in (None, "") and args.all_jobs:
            parser.error("speakr upload --status JOB cannot be combined with --all")
    setup_logging(args.verbose)
    if (args.command == "calendar" and args.calendar_command == "correct"):
        try:
            cfg = load_config()
        except Exception:
            cfg = None
    else:
        cfg = load_config()

    command = args.command or "run"
    if command == "calendar":
        if args.calendar_command == "correct" and args.clear and args.refresh:
            parser.error("calendar correct --clear cannot be combined with --refresh")
        if args.calendar_command == "correct":
            return _cmd_calendar_correct(cfg, args.recording, args.refresh,
                                         args.selector, args.clear)
        return _cmd_calendar(cfg, args.calendar_command,
                             getattr(args, "calendar_ids", None), getattr(args, "clear", False))
    if command == "speakr":
        assert cfg is not None
        if args.speakr_command == "upload":
            status_job = args.status if args.status else None
            status_all = args.status == ""
            if status_all and not args.all_jobs:
                parser.error("speakr upload --status requires --all when JOB is omitted")
            if status_job is not None and args.all_jobs:
                parser.error("speakr upload --status JOB cannot be combined with --all")
            relink_args = args.relink_args or (None, None)
            return _cmd_speakr_upload(
                cfg,
                args.path,
                all_jobs=args.all_jobs,
                status_job=status_job,
                status_all=status_all,
                retry_job=args.retry_job,
                relink_job=relink_args[0],
                relink_path=relink_args[1],
                forget_job=args.forget_job,
                force=args.force,
            )
        parser.error("unknown Speakr command")
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
