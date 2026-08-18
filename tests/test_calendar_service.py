"""Calendar worker lifecycle tests."""

from meeting_recorder.calendar_service import CalendarRefreshService


class _Refresher:
    def __init__(self): self.calls = []
    def refresh(self, ids, *, blocking): self.calls.append((ids, blocking))


def test_service_start_is_idempotent_and_bounded_stop_returns():
    refresher = _Refresher()
    service = CalendarRefreshService(refresher, lambda: (), interval_seconds=60)
    service.start(); service.start()
    assert service.stop(1)
