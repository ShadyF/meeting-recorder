"""Independent selected-calendar refresh tests."""

from datetime import datetime, timezone

from meeting_recorder.calendar_cache import CalendarCache
from meeting_recorder.calendar_refresh import CalendarRefresher


class _Client:
    def __init__(self): self.calls = []
    def list_occurrences(self, calendar_id, start, end):
        self.calls.append((calendar_id, start, end))
        if calendar_id == "bad": raise __import__("meeting_recorder.calendar_google", fromlist=["CalendarApiError"]).CalendarApiError("x", transient=False)
        return ()


def test_empty_refresh_has_no_token_or_network_work():
    client = _Client()
    report = CalendarRefresher(client, CalendarCache("/tmp/calendar-refresh-test"), lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)).refresh(())
    assert report.success and not client.calls
