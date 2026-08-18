"""Injected Google Calendar client checks with no live credentials or network."""

import threading
import urllib.parse
from datetime import datetime, timezone
from unittest.mock import patch

from meeting_recorder import calendar_google as google


def _event(event_id, **overrides):
    event = {
        "id": event_id,
        "status": "confirmed",
        "start": {"dateTime": "2026-03-08T01:30:00", "timeZone": "America/New_York"},
        "end": {"dateTime": "2026-03-08T03:30:00", "timeZone": "America/New_York"},
    }
    event.update(overrides)
    return event


def _client(pages, tokens=("token",), sleep=lambda _delay: None):
    calls, token_calls = [], []
    token_values = iter(tokens)

    def access_token():
        token_calls.append(True)
        try:
            return next(token_values)
        except StopIteration:
            return tokens[-1]

    def request(url, headers, timeout):
        calls.append((url, headers, timeout))
        return pages.pop(0)

    return google.GoogleCalendarClient(access_token, request, sleep, lambda: 0.0), calls, token_calls


def _raises_cancelled(callback):
    try:
        callback()
    except google.CalendarRefreshCancelled:
        return
    raise AssertionError("expected CalendarRefreshCancelled")


def test_list_operations_reuse_one_token_across_pages_and_refresh_only_one_401():
    client, calls, token_calls = _client([
        (200, {"items": [], "nextPageToken": "next"}, {}),
        (200, {"items": []}, {}),
    ])
    assert client.list_calendars() == []
    assert len(calls) == 2 and len(token_calls) == 1

    client, calls, token_calls = _client([
        (401, {}, {}),
        (200, {"items": [], "nextPageToken": "next"}, {}),
        (200, {"items": []}, {}),
    ], tokens=("old", "fresh"))
    assert client.list_calendars() == []
    assert len(token_calls) == 2
    assert [headers["Authorization"] for _url, headers, _timeout in calls] == [
        "Bearer old", "Bearer fresh", "Bearer fresh"]


def test_list_occurrences_uses_exact_params_and_skips_malformed_siblings():
    client, calls, _token_calls = _client([(200, {"items": [None, [], _event("valid")]}, {})])
    start = datetime(2026, 3, 8, 1, tzinfo=timezone.utc)
    end = datetime(2026, 3, 8, 2, tzinfo=timezone.utc)
    occurrences = client.list_occurrences("opaque/id", start, end)
    assert [item.key.event_id for item in occurrences] == ["valid"]
    split = urllib.parse.urlsplit(calls[0][0])
    assert "/calendars/opaque%2Fid/events" in split.path
    assert dict(urllib.parse.parse_qsl(split.query)) == {
        "singleEvents": "true", "orderBy": "startTime", "timeMin": "2026-03-08T01:00:00Z",
        "timeMax": "2026-03-08T02:00:00Z", "timeZone": "UTC", "showDeleted": "false",
        "showHiddenInvitations": "true", "maxResults": "2500",
    }


def test_normalization_is_total_and_stabilizes_private_metadata_and_people():
    for invalid_start in (None, "text", [], 1, {"date": "2026-03-08"}):
        assert google.normalize_event("calendar", _event("bad", start=invalid_start)) is None
    normalized = google.normalize_event("calendar", _event(
        "event", summary="  ", attendees=[{"displayName": "Zed"}, {"email": "a@example.test"},
                                                 {"displayName": " zed "}]))
    assert normalized is not None and normalized.summary is None
    assert [(person.email, person.display_name) for person in normalized.participants] == [
        (None, "Zed"), ("a@example.test", None)]


def test_event_cancellation_stops_pagination_without_a_second_request():
    cancel = threading.Event()
    calls = []

    def request(_url, _headers, _timeout):
        calls.append(True)
        cancel.set()
        return 200, {"items": [], "nextPageToken": "next"}, {}

    client = google.GoogleCalendarClient(lambda: "token", request)
    _raises_cancelled(lambda: client.list_calendars(cancel=cancel))
    assert len(calls) == 1


def test_event_wait_cancels_retry_after_without_sleep_or_followup_request():
    class CancellingEvent(threading.Event):
        def __init__(self):
            super().__init__()
            self.delays = []

        def wait(self, timeout=None):
            self.delays.append(timeout)
            self.set()
            return True

    cancel = CancellingEvent()
    ordinary_sleeps, calls = [], []

    def request(_url, _headers, _timeout):
        calls.append(True)
        return 403, {"error": "rate_limit_exceeded"}, {"Retry-After": "30"}

    client = google.GoogleCalendarClient(lambda: "token", request, ordinary_sleeps.append, lambda: 0.0)
    _raises_cancelled(lambda: client.list_calendars(cancel=cancel))
    assert cancel.delays == [30.0] and not ordinary_sleeps and len(calls) == 1


def test_production_transport_rejects_invalid_utf8_json_and_oversized_bodies_redacted():
    class Response:
        status = 200
        headers = {}

        def __init__(self, body):
            self.body = body
            self.limit = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            self.limit = limit
            return self.body

    for body in (b"\xff", b"{", b"x" * (google._MAX_RESPONSE_BYTES + 1)):
        response = Response(body)
        with patch("meeting_recorder.calendar_google.urllib.request.urlopen", return_value=response):
            try:
                google._production_request_json("https://example.invalid", {"Authorization": "Bearer private"}, 1)
            except google.CalendarApiError as exc:
                assert "private" not in str(exc).lower()
            else:
                raise AssertionError("invalid response was accepted")
        assert response.limit == google._MAX_RESPONSE_BYTES + 1
