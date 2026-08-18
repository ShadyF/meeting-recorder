"""Strict sidecar schema and durable adjacent-file behavior."""

import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import meeting_recorder.meeting_sidecar as sidecars
from meeting_recorder.calendar_domain import CalendarOccurrence, OccurrenceKey, meeting_snapshot
from meeting_recorder.meeting_sidecar import (
    MeetingSidecar, decode_sidecar, encode_sidecar, load_sidecar, remove_sidecar,
    sidecar_path, write_sidecar,
)


NOW = datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc)


def _meeting(summary: str = "Review"):
    occurrence = CalendarOccurrence(
        OccurrenceKey.recurring("work", "series", NOW), NOW,
        NOW + timedelta(minutes=45), summary=summary, details_visible=True)
    return meeting_snapshot(occurrence)


def _sidecar(name: str = "capture.mkv", meeting=None) -> MeetingSidecar:
    return MeetingSidecar(name, "capture.mkv", NOW, NOW + timedelta(minutes=1), meeting)


def test_sidecar_schema_round_trip_contains_selector_and_current_metadata() -> None:
    sidecar = _sidecar(meeting=_meeting())
    payload = encode_sidecar(sidecar)
    assert payload["schema_version"] == 1
    assert payload["recording_filename"] == "capture.mkv"
    assert payload["meeting"]["selector"]
    assert payload["meeting"]["title"] == "Review"
    assert decode_sidecar(payload) == sidecar


def test_sidecar_rejects_paths_bad_intervals_and_malformed_schema() -> None:
    for bad in ("../capture.mkv", "dir/capture.mkv", r"dir\capture.mkv", ""):
        try:
            _sidecar(bad)
            assert False, "path-bearing sidecar names must be rejected"
        except ValueError:
            pass
    try:
        MeetingSidecar("capture.mkv", "capture.mkv", NOW, NOW - timedelta(seconds=1), None)
        assert False, "reversed capture interval must be rejected"
    except ValueError:
        pass
    try:
        decode_sidecar({"schema_version": 99})
        assert False, "unknown schema must be rejected"
    except ValueError:
        pass


def test_sidecar_atomic_write_is_mode_0600_and_cleans_failed_temp() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        destination = sidecar_path(root / "capture.mkv")
        write_sidecar(destination, _sidecar())
        assert stat.S_IMODE(os.stat(destination).st_mode) == 0o600
        assert load_sidecar(destination) == _sidecar()

        original_replace = sidecars.os.replace
        try:
            def fail_replace(*_args, **_kwargs):
                raise OSError("simulated replace failure")

            sidecars.os.replace = fail_replace
            try:
                write_sidecar(destination, _sidecar("other.mkv"))
                assert False, "replace failure must propagate"
            except OSError:
                pass
        finally:
            sidecars.os.replace = original_replace
        assert not list(root.glob(".*.tmp"))


def test_sidecar_directory_fsync_errors_propagate_except_unsupported() -> None:
    with TemporaryDirectory() as directory:
        destination = sidecar_path(Path(directory) / "capture.mkv")
        original = sidecars._fsync_directory
        try:
            sidecars._fsync_directory = lambda _path: (_ for _ in ()).throw(OSError("fsync"))
            try:
                write_sidecar(destination, _sidecar())
                assert False, "real directory fsync errors must propagate"
            except OSError:
                pass
        finally:
            sidecars._fsync_directory = original


def test_sidecar_safe_remove_rejects_symlinks_and_syncs_directory() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        target = sidecar_path(root / "capture.mkv")
        write_sidecar(target, _sidecar())
        calls = []
        original_sync = sidecars._fsync_directory
        sidecars._fsync_directory = lambda path: calls.append(path)
        try:
            assert remove_sidecar(target)
        finally:
            sidecars._fsync_directory = original_sync
        assert not target.exists() and calls == [root]

        target.symlink_to(root / "other")
        try:
            remove_sidecar(target)
            assert False, "symlink removal must be rejected"
        except ValueError:
            pass


def test_sidecar_load_and_replace_refuse_symlink_targets() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        target = sidecar_path(root / "capture.mkv")
        target.symlink_to(root / "missing")
        for operation in (lambda: load_sidecar(target),
                          lambda: write_sidecar(target, _sidecar())):
            try:
                operation()
                assert False, "sidecar symlinks must be rejected"
            except (OSError, ValueError):
                pass
