"""Injected Google Calendar client checks with no live credentials or network."""

import urllib.parse
from datetime import datetime, timezone

from meeting_recorder import calendar_google as google


def _client(pages, *, tokens=("first",), cancelled=lambda: False, sleep=lambda _delay: None):
    calls = []
    token_values = iter(tokens)

    def access_token():
        try:
            return next(token_values)
        except StopIteration:
            return tokens[-1]

    def request(url, headers, timeout):
        calls.append((url, headers, timeout))
        return pages.pop(0)

    return google.GoogleCalendarClient(access_token, request, sleep, lambda: 0.0, cancelled), calls


def test_list_calendars_paginates_includes_hidden_and_excludes_deleted_without_selected():
    client, calls = _client([
        (200, {"items": [
            {"id": "z", "summary": "Zulu", "hidden": True, "selected": False},
            {"id": "gone", "summary": "Gone", "deleted": True},
        ], "nextPageToken": "next"}, {}),
        (200, {"items": [{"id": "a", "summary": "Alpha", "primary": True,
                            "selected": False}]}, {}),
    ])
    calendars = client.list_calendars()
    assert [item.id for item in calendars] == ["a", "z"]
    first = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(calls[0][0]).query))
    assert first == {"maxResults": "250", "showHidden": "true"}


def test_list_occurrences_uses_exact_utc_params_opaque_path_and_no_sync_token():
    client, calls = _client([(200, {"items": [_event("event")]}, {})])
    start = datetime(2026, 3, 8, 1, 0, tzinfo=timezone.utc)
    end = datetime(2026, 3, 8, 2, 0, tzinfo=timezone.utc)
    occurrences = client.list_occurrences("opaque/id", start, end)
    assert occurrences[0].key.event_id == "event"
    split = urllib.parse.urlsplit(calls[0][0])
    assert "/calendars/opaque%2Fid/events" in split.path
    params = dict(urllib.parse.parse_qsl(split.query))
    assert params == {
        "singleEvents": "true", "orderBy": "startTime", "timeMin": "2026-03-08T01:00:00Z",
        "timeMax": "2026-03-08T02:00:00Z", "timeZone": "UTC", "showDeleted": "false",
        "showHiddenInvitations": "true", "maxResults": "2500",
    }


def _event(event_id, **overrides):
    event = {
        "id": event_id,
        "status": "confirmed",
        "start": {"dateTime": "2026-03-08T01:30:00", "timeZone": "America/New_York"},
        "end": {"dateTime": "2026-03-08T03:30:00", "timeZone": "America/New_York"},
        "attendees": [{"email": "person@example.test", "displayName": "Person"}],
    }
    event.update(overrides)
    return event


def test_normalize_event_handles_moved_recurrence_dst_sparse_people_and_exclusions():
    moved = _event("instance", recurringEventId="series", originalStartTime={
        "dateTime": "2026-03-08T00:30:00-05:00"}, organizer={"email": "host@example.test"})
    occurrence = google.normalize_event("calendar", moved)
    assert occurrence.key.event_id == "series"
    assert occurrence.key.original_start_utc == datetime(2026, 3, 8, 5, 30, tzinfo=timezone.utc)
    assert occurrence.start_utc == datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc)
    assert occurrence.participants[0].email == "host@example.test"
    assert google.normalize_event("calendar", _event("all-day", start={"date": "2026-03-08"})) is None
    assert google.normalize_event("calendar", _event("cancelled", status="cancelled")) is None
    assert google.normalize_event("calendar", _event("declined", attendees=[{
        "self": True, "responseStatus": "declined"}])) is None
    assert google.normalize_event("calendar", _event("naive", start={"dateTime": "2026-03-08T01:00:00"})) is None
    assert google.normalize_event("calendar", _event("bad-series", recurringEventId="series")) is None


def test_normalize_event_retains_only_nondeclined_people_and_private_completeness_state():
    event = _event("private", attendeesOmitted=True, attendees=[
        {"email": "keep@example.test"}, {"email": "declined@example.test", "responseStatus": "declined"},
    ], organizer={"email": "host@example.test", "displayName": "Host"})
    occurrence = google.normalize_event("calendar", event)
    assert occurrence.participants[0].display_name == "Host"
    assert occurrence.participants[1].email == "keep@example.test"
    sparse = google.normalize_event("calendar", _event("sparse", attendees=None, organizer=None))
    assert sparse.participants == ()


def test_pagination_guards_and_transient_retry_401_and_cancellation_are_bounded():
    client, _calls = _client([(200, {"items": [], "nextPageToken": "repeat"}, {}),
                              (200, {"items": [], "nextPageToken": "repeat"}, {})])
    try:
        client.list_calendars()
    except google.CalendarApiError as exc:
        assert not exc.transient
    else:
        raise AssertionError("repeated page token was accepted")

    original_limit = google._MAX_PAGES
    google._MAX_PAGES = 1
    try:
        capped, _calls = _client([(200, {"items": [], "nextPageToken": "more"}, {})])
        try:
            capped.list_calendars()
        except google.CalendarApiError as exc:
            assert not exc.transient
        else:
            raise AssertionError("page cap was accepted")
    finally:
        google._MAX_PAGES = original_limit

    sleeps = []
    client, calls = _client([(401, {}, {}), (503, None, {}), (200, {"items": []}, {})],
                            tokens=("old", "new"), sleep=sleeps.append)
    assert client.list_calendars() == []
    assert [headers["Authorization"] for _url, headers, _timeout in calls] == [
        "Bearer old", "Bearer new", "Bearer new"]
    assert sleeps == [1.0]

    rate_sleeps = []
    rate_limited, _calls = _client([
        (403, {"error": "rate_limit_exceeded"}, {"Retry-After": "999"}),
        (200, {"items": []}, {}),
    ], sleep=rate_sleeps.append)
    assert rate_limited.list_calendars() == []
    assert rate_sleeps == [30.0]

    attempts = []
    def network_then_success(_url, _headers, _timeout):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("offline")
        return 200, {"items": []}, {}

    network_sleeps = []
    network = google.GoogleCalendarClient(lambda: "token", network_then_success,
                                           network_sleeps.append, lambda: 0.0)
    assert network.list_calendars() == []
    assert len(attempts) == 2 and network_sleeps == [1.0]

    cancelled = google.GoogleCalendarClient(lambda: "token", lambda *_args: (_ for _ in ()).throw(
        AssertionError("request should not run")), lambda _delay: None, lambda: 0.0, lambda: True)
    try:
        cancelled.list_calendars()
    except google.CalendarRefreshCancelled:
        pass
    else:
        raise AssertionError("cancelled refresh made a request")

    checks = iter((False, True))
    sleeps = []
    retry_cancelled = google.GoogleCalendarClient(lambda: "token", lambda *_args: (503, None, {}),
                                                   sleeps.append, lambda: 0.0, lambda: next(checks))
    try:
        retry_cancelled.list_calendars()
    except google.CalendarRefreshCancelled:
        pass
    else:
        raise AssertionError("cancellation before retry sleep was ignored")
    assert not sleeps


def test_transport_redacts_headers_and_bodies_from_errors():
    client, _calls = _client([(400, {"error": {"message": "private event body"}}, {})])
    try:
        client.list_calendars()
    except google.CalendarApiError as exc:
        assert "private" not in str(exc).lower()
        assert "bearer" not in str(exc).lower()
    else:
        raise AssertionError("permanent response was accepted")


def test_malformed_pages_abort_without_returning_partial_data_or_private_fields():
    client, _calls = _client([
        (200, {"items": [_event("first")], "nextPageToken": "next"}, {}),
        (200, {"items": "private malformed body"}, {}),
    ])
    try:
        client.list_occurrences(
            "calendar", datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc))
    except google.CalendarApiError as exc:
        assert "private" not in str(exc).lower()
    else:
        raise AssertionError("malformed page returned partial occurrences")
