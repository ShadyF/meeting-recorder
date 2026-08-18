"""Recording filename, collision, lock, and no-replace move behavior."""

import errno
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import meeting_recorder.recording_paths as paths
from meeting_recorder.calendar_domain import CalendarOccurrence, OccurrenceKey
from meeting_recorder.meeting_sidecar import sidecar_path
from meeting_recorder.recording_paths import (
    collision_safe_path, move_regular_file_no_replace, recording_directory_lock,
    sanitize_title, truncate_utf8, visible_recording_filename,
)


def _occurrence(summary: str = "Planning / Café") -> CalendarOccurrence:
    start = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    return CalendarOccurrence(OccurrenceKey.single("calendar", "event"), start,
                              datetime(2026, 11, 1, 6, tzinfo=timezone.utc),
                              summary=summary, details_visible=True)


def test_unicode_nfc_whitespace_safe_runs_and_utf8_truncation() -> None:
    assert sanitize_title("  Cafe\u0301 / design   review  ") == "Café_design_review".replace("é", "é")
    assert sanitize_title("///") == "Meeting"
    long_title = "é" * 100
    result = truncate_utf8(sanitize_title(long_title), 120)
    assert len(result.encode("utf-8")) <= 120 and result.encode("utf-8").decode("utf-8") == result


def test_visible_filename_uses_injected_local_time_at_event_instant() -> None:
    occurrence = _occurrence("DST injection")
    local = lambda value: value.astimezone(ZoneInfo("America/New_York"))
    filename = visible_recording_filename(occurrence, Path("recording.tar.mkv"), local)
    assert filename == "2026-11-01_01-30-00_DST_injection.tar.mkv"


def test_collision_suffix_checks_media_sidecar_and_broken_symlink() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        preferred = root / "2026-01-01_00-00-00_Meeting.mkv"
        preferred.write_bytes(b"one")
        sidecar_path(preferred.with_name(preferred.name[:-4] + "-2.mkv")).write_bytes(b"sidecar")
        broken = preferred.with_name(preferred.name[:-4] + "-3.mkv")
        broken.symlink_to(root / "missing")
        assert collision_safe_path(preferred).name.endswith("-4.mkv")


def test_recording_directory_lock_is_private_and_releases_fd() -> None:
    with TemporaryDirectory() as directory, TemporaryDirectory() as cache:
        original = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = cache
        try:
            with recording_directory_lock(directory):
                pass
            locks = list(Path(cache).rglob("*.lock"))
            assert len(locks) == 1 and stat_mode(locks[0]) == 0o600
        finally:
            if original is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = original


def test_recording_directory_lock_serializes_concurrent_transactions() -> None:
    with TemporaryDirectory() as directory, TemporaryDirectory() as cache:
        original = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = cache
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def holder() -> None:
            with recording_directory_lock(directory):
                entered.set()
                release.wait(2)

        def waiter() -> None:
            with recording_directory_lock(directory):
                finished.set()

        first = threading.Thread(target=holder)
        second = threading.Thread(target=waiter)
        first.start()
        assert entered.wait(2)
        second.start()
        assert not finished.wait(0.05)
        release.set()
        first.join(2)
        second.join(2)
        try:
            assert finished.is_set()
        finally:
            if original is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = original


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def test_move_regular_file_no_replace_and_rejects_cross_directory() -> None:
    with TemporaryDirectory() as first, TemporaryDirectory() as second:
        source = Path(first) / "recording.mkv"
        destination = Path(first) / "moved.mkv"
        source.write_bytes(b"data")
        assert move_regular_file_no_replace(source, destination) == destination
        assert destination.read_bytes() == b"data" and not source.exists()
        existing_source = Path(first) / "existing-source.mkv"
        existing_destination = Path(first) / "existing-destination.mkv"
        existing_source.write_bytes(b"source")
        existing_destination.write_bytes(b"existing")
        try:
            move_regular_file_no_replace(existing_source, existing_destination)
            assert False, "existing destination must not be replaced"
        except FileExistsError:
            pass
        source = Path(first) / "again.mkv"
        source.write_bytes(b"data")
        try:
            move_regular_file_no_replace(source, Path(second) / "other.mkv")
            assert False, "cross-directory move must be rejected"
        except ValueError:
            pass
        assert source.exists()


def test_move_rolls_back_verified_hardlink_when_source_unlink_fails() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source, destination = root / "source.mkv", root / "destination.mkv"
        source.write_bytes(b"authoritative")
        original_unlink = paths.os.unlink
        calls = []

        def fail_source(path, *args, **kwargs):
            calls.append(path)
            if Path(path) == source:
                raise OSError(errno.EIO, "simulated source unlink failure")
            return original_unlink(path, *args, **kwargs)

        paths.os.unlink = fail_source
        try:
            try:
                move_regular_file_no_replace(source, destination)
                assert False, "source unlink failure must propagate"
            except OSError:
                pass
        finally:
            paths.os.unlink = original_unlink
        assert source.exists() and not destination.exists()
