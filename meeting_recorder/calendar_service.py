"""One cancellable background Calendar refresh worker."""

from __future__ import annotations

import logging
import threading
from typing import Callable

from .calendar_refresh import CalendarRefresher


LOG = logging.getLogger(__name__)


class CalendarRefreshService:
    def __init__(self, refresher: CalendarRefresher, selected_ids: Callable[[], tuple[str, ...]],
                 interval_seconds: float = 900) -> None:
        self._refresher, self._selected_ids, self._interval = refresher, selected_ids, interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="calendar-refresh", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresher.refresh(self._selected_ids(), blocking=False, cancel=self._stop)
            except Exception:
                LOG.warning("Calendar background refresh failed", exc_info=True)
            if self._stop.wait(self._interval):
                return

    def stop(self, timeout: float) -> bool:
        self._stop.set()
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()
