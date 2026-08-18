"""Calendar worker lifecycle tests."""

import threading

from meeting_recorder.calendar_service import CalendarRefreshService


class _Refresher:
    def __init__(self): self.calls = []
    def refresh(self, ids, *, blocking, cancel): self.calls.append((ids, blocking, cancel))


def test_service_start_is_idempotent_and_bounded_stop_returns():
    refresher = _Refresher()
    service = CalendarRefreshService(refresher, lambda: (), interval_seconds=60)
    service.start(); service.start()
    assert service.stop(1)


def test_service_refreshes_immediately_reloads_selection_and_waits_between_cycles():
    class CyclingStop:
        def __init__(self):
            self.set_calls = 0
            self.waits = []

        def clear(self):
            self.set_calls = 0

        def is_set(self):
            return self.set_calls > 0

        def set(self):
            self.set_calls += 1

        def wait(self, timeout=None):
            self.waits.append(timeout)
            if len(self.waits) == 2:
                self.set()
                return True
            return False

    class Refresher:
        def __init__(self): self.calls = []
        def refresh(self, ids, *, blocking, cancel): self.calls.append((ids, blocking, cancel))

    selections = iter((("first",), ("second",)))
    refresher = Refresher()
    service = CalendarRefreshService(refresher, lambda: next(selections), interval_seconds=900)
    service._stop = CyclingStop()
    service.start()
    service._thread.join(1)
    assert [call[:2] for call in refresher.calls] == [(('first',), False), (('second',), False)]
    assert service._stop.waits == [900, 900]


def test_service_stop_propagates_exact_event_and_reports_blocked_worker():
    entered, release = threading.Event(), threading.Event()

    class BlockingRefresher:
        def __init__(self): self.cancel = None
        def refresh(self, _ids, *, blocking, cancel):
            self.cancel = cancel
            entered.set()
            release.wait()

    refresher = BlockingRefresher()
    service = CalendarRefreshService(refresher, lambda: ("one",), interval_seconds=900)
    service.start()
    assert entered.wait(1) and refresher.cancel is service._stop
    assert not service.stop(0)
    release.set()
    assert service.stop(1)
