"""Independent selected-calendar snapshot refreshes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event, Lock
from typing import Callable, Sequence

from .calendar_cache import CACHE_VERSION, CalendarCache, CalendarSnapshot, calendar_operation_lock, snapshot_window
from .calendar_google import CalendarApiError, CalendarRefreshCancelled, GoogleCalendarClient


@dataclass(frozen=True)
class CalendarRefreshResult:
    calendar_id: str
    success: bool
    cancelled: bool = False
    detail: str = ""


@dataclass(frozen=True)
class CalendarRefreshReport:
    results: tuple[CalendarRefreshResult, ...]
    already_in_progress: bool = False

    @property
    def success(self) -> bool:
        return not self.already_in_progress and all(result.success for result in self.results)


class CalendarRefresher:
    """Fetch and commit each selected calendar independently under one operation lock."""

    def __init__(self, client: GoogleCalendarClient, cache: CalendarCache,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 before_commit: Callable[[], None] = lambda: None) -> None:
        self.client, self.cache, self._now = client, cache, now
        self._before_commit = before_commit
        self._commit_gate = Lock()

    def request_cancel(self, cancel: Event, timeout: float) -> bool:
        """Signal cancellation immediately, then wait only briefly for a commit to settle."""
        cancel.set()
        acquired = self._commit_gate.acquire(timeout=max(0.0, timeout))
        if not acquired:
            return False
        try:
            return True
        finally:
            self._commit_gate.release()

    def refresh(self, calendar_ids: Sequence[str], *, blocking: bool = False,
                cancel: Event | None = None) -> CalendarRefreshReport:
        if not calendar_ids:
            return CalendarRefreshReport(())
        now = self._now()
        start, end = snapshot_window(now)
        with calendar_operation_lock(blocking=blocking, root=self.cache.root) as acquired:
            if not acquired:
                return CalendarRefreshReport((), already_in_progress=True)
            results = []
            for calendar_id in calendar_ids:
                try:
                    if cancel is not None and cancel.is_set():
                        raise CalendarRefreshCancelled()
                    occurrences = self.client.list_occurrences(calendar_id, start, end, cancel=cancel)
                    if cancel is not None and cancel.is_set():
                        raise CalendarRefreshCancelled()
                    self._before_commit()

                    # Make cancellation and the atomic cache replacement one ordered operation.
                    with self._commit_gate:
                        if cancel is not None and cancel.is_set():
                            raise CalendarRefreshCancelled()
                        self.cache.store(CalendarSnapshot(CACHE_VERSION, calendar_id, now, start, end, occurrences))
                    results.append(CalendarRefreshResult(calendar_id, True))
                except CalendarRefreshCancelled:
                    results.append(CalendarRefreshResult(calendar_id, False, True, "cancelled"))
                    break
                except (CalendarApiError, OSError, ValueError):
                    results.append(CalendarRefreshResult(calendar_id, False, detail="unavailable"))
        return CalendarRefreshReport(tuple(results))
