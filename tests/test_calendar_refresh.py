"""Independent selected-calendar refresh tests."""

import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from meeting_recorder.calendar_cache import CalendarCache, CalendarSnapshot, calendar_operation_lock, snapshot_window
from meeting_recorder.calendar_domain import CalendarOccurrence, OccurrenceKey
from meeting_recorder.calendar_google import CalendarApiError
from meeting_recorder.calendar_refresh import CalendarRefresher


class _Client:
    def __init__(self): self.calls = []
    def list_occurrences(self, calendar_id, start, end, *, cancel=None):
        self.calls.append((calendar_id, start, end))
        if calendar_id == "bad": raise CalendarApiError("x", transient=False)
        return ()


def test_empty_refresh_has_no_token_or_network_work():
    client = _Client()
    with tempfile.TemporaryDirectory() as temporary:
        report = CalendarRefresher(client, CalendarCache(Path(temporary)),
                                   lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)).refresh(())
        assert report.success and not client.calls


def test_refresh_uses_exact_shared_window_for_each_selected_calendar():
    now = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)
    client = _Client()
    with tempfile.TemporaryDirectory() as temporary:
        CalendarRefresher(client, CalendarCache(Path(temporary)), lambda: now).refresh(("one", "two"), blocking=True)
    assert client.calls == [("one", now - timedelta(hours=24), now + timedelta(days=7)),
                            ("two", now - timedelta(hours=24), now + timedelta(days=7))]


def test_refresh_isolates_store_and_api_failures_without_replacing_old_snapshots():
    now = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)

    class StoreFailsOnce(CalendarCache):
        def __init__(self, root):
            super().__init__(root)
            self.fail = True

        def store(self, snapshot):
            if self.fail and snapshot.calendar_id == "one":
                self.fail = False
                raise OSError("disk full")
            return super().store(snapshot)

    class ApiFails(_Client):
        def list_occurrences(self, calendar_id, start, end, *, cancel=None):
            self.calls.append((calendar_id, start, end))
            if calendar_id == "api":
                raise CalendarApiError("offline", transient=True)
            return ()

    with tempfile.TemporaryDirectory() as temporary:
        cache = StoreFailsOnce(Path(temporary))
        start, end = snapshot_window(now)
        old = CalendarSnapshot(1, "one", now - timedelta(hours=1), start, end, ())
        cache.fail = False
        cache.store(old)
        cache.fail = True
        report = CalendarRefresher(_Client(), cache, lambda: now).refresh(("one", "two"), blocking=True)
        assert [result.success for result in report.results] == [False, True]
        assert cache.load("one") == old and cache.load("two") is not None
        report = CalendarRefresher(ApiFails(), cache, lambda: now).refresh(("api", "after"), blocking=True)
        assert [result.success for result in report.results] == [False, True]
        assert cache.load("one") == old and cache.load("after") is not None


def test_nonblocking_refresh_reports_in_progress_without_network():
    client = _Client()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "cache"
        with calendar_operation_lock(blocking=True, root=root):
            report = CalendarRefresher(client, CalendarCache(root)).refresh(("one",), blocking=False)
    assert report.already_in_progress and not report.results and not client.calls


def test_cancellation_after_request_prevents_store_and_later_calendars():
    now = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)
    stop = threading.Event()

    class CancellingClient(_Client):
        def list_occurrences(self, calendar_id, start, end, *, cancel=None):
            self.calls.append((calendar_id, start, end))
            stop.set()
            return (CalendarOccurrence(OccurrenceKey(calendar_id, "event"), now,
                                       now + timedelta(hours=1)),)

    with tempfile.TemporaryDirectory() as temporary:
        cache = CalendarCache(Path(temporary))
        report = CalendarRefresher(CancellingClient(), cache, lambda: now).refresh(
            ("one", "two"), blocking=True, cancel=stop)
        assert report.results[0].cancelled and len(report.results) == 1
        assert cache.load("one") is None and cache.load("two") is None
