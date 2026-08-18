"""Frozen Calendar domain contract tests."""

from datetime import datetime, timedelta, timezone

from meeting_recorder.calendar_domain import (
    CalendarOccurrence, CalendarParticipant, OccurrenceKey, is_event_eligible,
    match_occurrence, normalize_participants,
)


NOW = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)


def _event(name, start, end):
    return CalendarOccurrence(OccurrenceKey("calendar", name), start, end)


def test_eligibility_and_name_only_participants_follow_the_frozen_rules():
    assert is_event_eligible(all_day=False, status="confirmed", self_response_status=None)
    assert not is_event_eligible(all_day=True, status="confirmed", self_response_status=None)
    assert not is_event_eligible(all_day=False, status="cancelled", self_response_status=None)
    assert not is_event_eligible(all_day=False, status="tentative", self_response_status="declined")
    people = normalize_participants([{"email": " A@EXAMPLE.test ", "displayName": "  Ada   Lovelace "},
                                    {"email": "a@example.test", "displayName": "Better"},
                                    {"displayName": " Name Only "}])
    assert people == (CalendarParticipant("a@example.test", "Ada Lovelace"),
                      CalendarParticipant(None, "Name Only"))


def test_matching_uses_greatest_overlap_exact_boundaries_and_zero_length_capture():
    capture_end = NOW + timedelta(hours=1)
    small = _event("small", NOW, NOW + timedelta(minutes=5))
    large = _event("large", NOW + timedelta(minutes=20), NOW + timedelta(hours=2))
    match = match_occurrence(NOW, capture_end, [small, large])
    assert match is not None and match.occurrence.key.event_id == "large"
    boundary = _event("boundary", NOW - timedelta(minutes=10), NOW)
    assert match_occurrence(NOW, NOW, [boundary]) is not None
