"""Bounded Google Calendar reads and privacy-preserving event normalization."""

from __future__ import annotations

import http.client
import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .calendar_domain import (CalendarInfo, CalendarOccurrence, OccurrenceKey,
                              is_event_eligible, normalize_participants)


_API_ROOT = "https://www.googleapis.com/calendar/v3"
_MAX_PAGES = 100
_MAX_ATTEMPTS = 3
_TIMEOUT_SECONDS = 15
_MAX_RESPONSE_BYTES = 1024 * 1024


class CalendarApiError(RuntimeError):
    """A redacted Calendar API failure callers can classify without response data."""

    def __init__(self, message: str, *, transient: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.transient = transient
        self.status = status


class CalendarRefreshCancelled(RuntimeError):
    """Raised when a coordinating service stops a Calendar refresh."""


def _utc_timestamp(value: datetime) -> str:
    """Format an aware instant exactly as the Calendar API expects."""
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime | None:
    """Parse one timed Google value while rejecting all-day and naive values."""
    if not isinstance(value, dict) or not isinstance(value.get("dateTime"), str):
        return None
    try:
        parsed = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        zone_name = value.get("timeZone")
        if not isinstance(zone_name, str):
            return None
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(zone_name))
        except ZoneInfoNotFoundError:
            return None
    return parsed.astimezone(timezone.utc)


def _person(raw: object) -> dict[str, str | None] | None:
    """Keep only explicit attendee fields; never synthesize private identity data."""
    if not isinstance(raw, dict):
        return None
    email = raw.get("email")
    display_name = raw.get("displayName")
    if email is not None and not isinstance(email, str):
        return None
    if display_name is not None and not isinstance(display_name, str):
        return None
    if email is None and display_name is None:
        return None
    return {"email": email, "display_name": display_name}


def normalize_event(calendar_id: str, raw: object) -> CalendarOccurrence | None:
    """Normalize one eligible timed event; arbitrary JSON values simply do not match."""
    if not isinstance(calendar_id, str) or not calendar_id or not isinstance(raw, dict):
        return None
    attendees = raw.get("attendees")
    self_status = next((item.get("responseStatus") for item in attendees
                        if isinstance(item, dict) and item.get("self") is True), None) \
        if isinstance(attendees, list) else None
    start_value = raw.get("start")
    if not is_event_eligible(all_day=isinstance(start_value, dict) and "date" in start_value,
                             status=raw.get("status"), self_response_status=self_status):
        return None
    start = _parse_timestamp(start_value)
    end = _parse_timestamp(raw.get("end"))
    if start is None or end is None or end <= start:
        return None

    if isinstance(attendees, list):
        if any(item.get("self") is True and item.get("responseStatus") == "declined"
               for item in attendees if isinstance(item, dict)):
            return None
        people = [person for item in attendees
                  if isinstance(item, dict) and item.get("responseStatus") != "declined"
                  for person in [_person(item)] if person is not None]
    else:
        people = []

    organizer = _person(raw.get("organizer"))
    if organizer is not None:
        people.append(organizer)

    recurring_id = raw.get("recurringEventId")
    if recurring_id is not None:
        original_start = _parse_timestamp(raw.get("originalStartTime"))
        if not isinstance(recurring_id, str) or not recurring_id or original_start is None:
            return None
        key = OccurrenceKey.recurring(calendar_id, recurring_id, original_start)
    else:
        event_id = raw.get("id")
        if not isinstance(event_id, str) or not event_id:
            return None
        key = OccurrenceKey.single(calendar_id, event_id)

    raw_summary = raw.get("summary")
    summary = raw_summary.strip() if isinstance(raw_summary, str) and raw_summary.strip() else None
    raw_description = raw.get("description")
    description = raw_description.strip() if isinstance(raw_description, str) and raw_description.strip() else None
    raw_location = raw.get("location")
    location = raw_location.strip() if isinstance(raw_location, str) and raw_location.strip() else None
    complete = False if raw.get("attendeesOmitted") is True else (True if isinstance(attendees, list) else None)
    return CalendarOccurrence(key, start, end, summary, normalize_participants(people), complete,
                              description, location, summary is not None)


def _read_json(response: Any, status: int) -> Any:
    """Read a sane bounded body and turn every decode failure into a redacted error."""
    try:
        body = response.read(_MAX_RESPONSE_BYTES + 1)
    except (http.client.HTTPException, OSError, ValueError) as exc:
        raise CalendarApiError("Calendar response is unavailable", transient=True, status=status) from exc
    if not isinstance(body, bytes):
        raise CalendarApiError("Calendar response is malformed", transient=False, status=status)
    if len(body) > _MAX_RESPONSE_BYTES:
        raise CalendarApiError("Calendar response is too large", transient=False, status=status)
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise CalendarApiError("Calendar response is malformed", transient=False, status=status) from exc


def _production_request_json(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, Any, Mapping[str, str]]:
    """Issue one authenticated GET with finite timeout and no response logging."""
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _read_json(response, response.status), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        # Preserve status policy even when an error response body is unusable.
        try:
            body = _read_json(exc, exc.code)
        except CalendarApiError:
            body = None
        return exc.code, body, dict(exc.headers.items()) if exc.headers else {}
    except (http.client.HTTPException, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CalendarApiError("Calendar request is temporarily unavailable", transient=True) from exc


class GoogleCalendarClient:
    """Read bounded Calendar pages with injected transport, delay, and token providers."""

    def __init__(self, access_token: Callable[[], str],
                 request_json: Callable[[str, Mapping[str, str], float], Any] = _production_request_json,
                 sleep: Callable[[float], None] = time.sleep,
                 jitter: Callable[[], float] = random.random) -> None:
        self._access_token = access_token
        self._request_json = request_json
        self._sleep = sleep
        self._jitter = jitter

    @staticmethod
    def _check_cancelled(cancel: threading.Event | None) -> None:
        """Stop at every I/O boundary so cancellation cannot advance refresh work."""
        if cancel is not None and cancel.is_set():
            raise CalendarRefreshCancelled("Calendar refresh was cancelled")

    def _wait(self, delay: float, cancel: threading.Event | None) -> None:
        """Make service refresh delays interruptible without changing CLI sleep behavior."""
        if cancel is None:
            self._sleep(delay)
            return
        if cancel.wait(delay) or cancel.is_set():
            raise CalendarRefreshCancelled("Calendar refresh was cancelled")

    def _acquire_token(self) -> str:
        """Validate one operation-scoped access token without exposing its value."""
        token = self._access_token()
        if not isinstance(token, str) or not token:
            raise CalendarApiError("Calendar credential is unavailable", transient=False)
        return token

    @staticmethod
    def _unpack_response(response: Any) -> tuple[int, Any, Mapping[str, str]]:
        """Validate injected transport shape before any pagination state changes."""
        if not isinstance(response, tuple) or len(response) not in {2, 3}:
            raise CalendarApiError("Calendar response is malformed", transient=False)
        status, body = response[0], response[1]
        headers = response[2] if len(response) == 3 else {}
        if not isinstance(status, int) or not isinstance(headers, Mapping):
            raise CalendarApiError("Calendar response is malformed", transient=False)
        return status, body, headers

    @staticmethod
    def _retry_after(headers: Mapping[str, str]) -> float | None:
        """Honor a bounded Retry-After hint without retaining response headers."""
        value = next((value for key, value in headers.items() if key.lower() == "retry-after"), None)
        if not isinstance(value, str):
            return None
        try:
            return min(30.0, max(0.0, float(value)))
        except ValueError:
            try:
                return min(30.0, max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError):
                return None

    @staticmethod
    def _rate_limit_403(body: Any) -> bool:
        """Recognize known rate-limit envelopes without surfacing their message text."""
        if not isinstance(body, dict):
            return False
        error = body.get("error")
        if isinstance(error, str):
            return "rate" in error.lower() or "quota" in error.lower()
        if not isinstance(error, dict):
            return False
        text = " ".join(str(error.get(key, "")) for key in ("status", "message")).lower()
        details = error.get("errors", [])
        if isinstance(details, list):
            text += " " + " ".join(str(item.get("reason", "")) for item in details
                                     if isinstance(item, dict)).lower()
        return any(value in text for value in ("rate", "quota", "resource_exhausted"))

    def _get_page(self, url: str, token: str, token_refreshed: bool,
                  cancel: threading.Event | None) -> tuple[dict[str, Any], str, bool]:
        """Fetch one page with one reused token and bounded transient retries."""
        transient_attempts = 0
        for attempt in range(_MAX_ATTEMPTS):
            self._check_cancelled(cancel)
            try:
                response = self._request_json(url, {"Authorization": f"Bearer {token}"}, _TIMEOUT_SECONDS)
                status, body, headers = self._unpack_response(response)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt + 1 == _MAX_ATTEMPTS:
                    raise CalendarApiError("Calendar request is temporarily unavailable", transient=True) from exc
                self._wait(min(30.0, (2 ** transient_attempts) + self._jitter()), cancel)
                transient_attempts += 1
                continue
            except CalendarApiError as exc:
                if not exc.transient or attempt + 1 == _MAX_ATTEMPTS:
                    raise
                self._wait(min(30.0, (2 ** transient_attempts) + self._jitter()), cancel)
                transient_attempts += 1
                continue

            self._check_cancelled(cancel)
            if status == 401 and not token_refreshed:
                token = self._acquire_token()
                token_refreshed = True
                continue
            retryable = status in {408, 429} or status >= 500
            retryable = retryable or (status == 403 and self._rate_limit_403(body))
            if retryable:
                if attempt + 1 == _MAX_ATTEMPTS:
                    raise CalendarApiError("Calendar request is temporarily unavailable", transient=True, status=status)
                delay = self._retry_after(headers)
                self._wait(delay if delay is not None else min(
                    30.0, (2 ** transient_attempts) + self._jitter()), cancel)
                transient_attempts += 1
                continue
            if status != 200:
                raise CalendarApiError("Calendar request failed", transient=False, status=status)
            if not isinstance(body, dict):
                raise CalendarApiError("Calendar response is malformed", transient=False, status=status)
            return body, token, token_refreshed
        raise CalendarApiError("Calendar request is temporarily unavailable", transient=True)

    def _list_pages(self, path: str, params: dict[str, str],
                    cancel: threading.Event | None) -> list[object]:
        """Read one bounded sequence while retaining operation-scoped auth state."""
        self._check_cancelled(cancel)
        token = self._acquire_token()
        token_refreshed = False
        items_from_pages: list[object] = []
        seen_tokens: set[str] = set()
        page_token: str | None = None
        for _ in range(_MAX_PAGES):
            self._check_cancelled(cancel)
            query = dict(params)
            if page_token is not None:
                query["pageToken"] = page_token
            url = f"{_API_ROOT}{path}?{urllib.parse.urlencode(query)}"
            page, token, token_refreshed = self._get_page(url, token, token_refreshed, cancel)
            self._check_cancelled(cancel)
            items = page.get("items")
            if not isinstance(items, list):
                raise CalendarApiError("Calendar page is malformed", transient=False)
            items_from_pages.extend(items)
            next_token = page.get("nextPageToken")
            if next_token is None:
                return items_from_pages
            if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
                raise CalendarApiError("Calendar pagination is malformed", transient=False)
            seen_tokens.add(next_token)
            page_token = next_token
        raise CalendarApiError("Calendar pagination exceeded its page limit", transient=False)

    def list_calendars(self, *, cancel: threading.Event | None = None) -> list[CalendarInfo]:
        """List nondeleted calendars deterministically without consulting selected."""
        items = self._list_pages("/users/me/calendarList", {
            "maxResults": "250",
            "showHidden": "true",
        }, cancel)
        calendars = []
        for item in items:
            if not isinstance(item, dict):
                continue
            calendar_id = item.get("id")
            if item.get("deleted") is True or not isinstance(calendar_id, str) or not calendar_id:
                continue
            raw_summary = item.get("summary")
            summary = raw_summary.strip() if isinstance(raw_summary, str) and raw_summary.strip() else None
            access_role = item.get("accessRole") if isinstance(item.get("accessRole"), str) else None
            calendars.append(CalendarInfo(calendar_id, summary, item.get("primary") is True, access_role))
        return sorted(calendars, key=lambda calendar: ((calendar.summary or "").casefold(), calendar.id))

    def list_occurrences(self, calendar_id: str, time_min: datetime, time_max: datetime, *,
                         cancel: threading.Event | None = None) -> tuple[CalendarOccurrence, ...]:
        """List a UTC window of normalized occurrences without incremental sync state."""
        if not isinstance(calendar_id, str) or not calendar_id:
            raise ValueError("calendar_id must be a nonempty opaque ID")
        if time_min.tzinfo is None or time_max.tzinfo is None or time_max <= time_min:
            raise ValueError("occurrence window must be positive and timezone-aware")
        path = "/calendars/" + urllib.parse.quote(calendar_id, safe="") + "/events"
        items = self._list_pages(path, {
            "singleEvents": "true",
            "orderBy": "startTime",
            "timeMin": _utc_timestamp(time_min),
            "timeMax": _utc_timestamp(time_max),
            "timeZone": "UTC",
            "showDeleted": "false",
            "showHiddenInvitations": "true",
            "maxResults": "2500",
        }, cancel)
        return tuple(event for item in items if (event := normalize_event(calendar_id, item)) is not None)
