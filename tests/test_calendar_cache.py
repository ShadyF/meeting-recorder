"""Calendar snapshot cache contract tests."""

import json
import os
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from meeting_recorder.calendar_cache import (
    CACHE_VERSION, CalendarCache, CalendarSnapshot, calendar_operation_lock,
    is_snapshot_fresh, snapshot_window,
)
from meeting_recorder.calendar_domain import CalendarOccurrence, CalendarParticipant, OccurrenceKey


NOW = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)


def _snapshot(calendar_id="calendar"):
    start, end = snapshot_window(NOW)
    occurrence = CalendarOccurrence(OccurrenceKey(calendar_id, "event"), NOW, NOW + timedelta(hours=1),
                                    participants=(CalendarParticipant(None, "Name"),), participants_complete=False)
    return CalendarSnapshot(CACHE_VERSION, calendar_id, NOW, start, end, (occurrence,))


def test_snapshot_round_trip_keeps_private_completeness_and_hashes_opaque_ids():
    with tempfile.TemporaryDirectory() as temporary:
        cache = CalendarCache(Path(temporary) / "google-calendar")
        snapshot = _snapshot("../../opaque")
        cache.store(snapshot)
        assert cache.load("../../opaque") == snapshot
        assert stat.S_IMODE(cache.path_for("../../opaque").stat().st_mode) == 0o600
        assert cache.load("../../opaque").occurrences[0].participants_complete is False


def test_freshness_selection_and_lock_path_are_bounded():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "google-calendar"
        cache = CalendarCache(root)
        cache.store(_snapshot())
        assert is_snapshot_fresh(cache.load("calendar"), NOW)
        assert cache.load_selected_occurrences(["calendar"], NOW)
        with calendar_operation_lock(blocking=True, root=root) as acquired:
            assert acquired
        lock = root.parent / "calendar.lock"
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_snapshot_window_and_freshness_boundaries_reject_future_clock_skew():
    start, end = snapshot_window(NOW)
    assert start == NOW - timedelta(hours=24) and end == NOW + timedelta(days=7)
    snapshot = _snapshot()
    for age in (timedelta(days=6), timedelta(days=7)):
        assert is_snapshot_fresh(CalendarSnapshot(1, "calendar", NOW - age, start, end, ()), NOW)
    assert not is_snapshot_fresh(CalendarSnapshot(1, "calendar", NOW - timedelta(days=7, microseconds=1), start, end, ()), NOW)
    assert not is_snapshot_fresh(CalendarSnapshot(1, "calendar", NOW + timedelta(microseconds=1), start, end, ()), NOW)


def test_cache_treats_corrupt_versions_and_embedded_calendar_mismatches_as_misses():
    with tempfile.TemporaryDirectory() as temporary:
        cache = CalendarCache(Path(temporary) / "google-calendar")
        cache.root.mkdir()
        target = cache.path_for("calendar")
        for body in ("{", json.dumps({"version": 9, "occurrences": []}),
                     json.dumps({"version": 1, "calendar_id": "other", "occurrences": []})):
            target.write_text(body, encoding="utf-8")
            assert cache.load("calendar") is None


def test_cache_atomic_replace_failure_preserves_old_snapshot_removes_temp_and_fsyncs_directory():
    with tempfile.TemporaryDirectory() as temporary:
        cache = CalendarCache(Path(temporary) / "google-calendar")
        old = _snapshot()
        cache.store(old)
        replacement = CalendarSnapshot(1, "calendar", NOW + timedelta(hours=1), *snapshot_window(NOW), ())
        with patch("meeting_recorder.calendar_cache.os.replace", side_effect=OSError("disk full")):
            try:
                cache.store(replacement)
            except OSError:
                pass
            else:
                raise AssertionError("replace failure was hidden")
        assert cache.load("calendar") == old
        assert not list(cache.root.glob(".calendar-write-*.tmp"))
        attempted = []
        with patch("meeting_recorder.calendar_cache._fsync_directory", side_effect=attempted.append):
            cache.store(replacement)
        assert attempted == [cache.root]


def test_cache_lock_handles_nonblocking_contention_and_closes_generic_flock_failures():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "google-calendar"
        with calendar_operation_lock(blocking=True, root=root):
            with calendar_operation_lock(blocking=False, root=root) as acquired:
                assert not acquired
        real_close, closed = os.close, []
        with patch("meeting_recorder.calendar_cache.fcntl.flock", side_effect=OSError("bad lock")), \
             patch("meeting_recorder.calendar_cache.os.close", side_effect=lambda fd: closed.append(fd) or real_close(fd)):
            try:
                with calendar_operation_lock(blocking=True, root=root):
                    pass
            except OSError:
                pass
            else:
                raise AssertionError("generic flock failure was hidden")
        assert closed
