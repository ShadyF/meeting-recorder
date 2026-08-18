"""Calendar snapshot cache contract tests."""

import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
