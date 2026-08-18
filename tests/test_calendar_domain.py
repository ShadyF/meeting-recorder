"""Frozen Calendar domain contract tests."""

from datetime import datetime, timedelta, timezone

from meeting_recorder.calendar_domain import (
    CalendarMatch, CalendarOccurrence, CalendarParticipant, MeetingSnapshot, OccurrenceKey,
    decode_occurrence_selector, encode_occurrence_selector, is_event_eligible,
    match_occurrence, meeting_snapshot, normalize_participants,
)


NOW = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)


def _event(event_id, start, end, **metadata):
    return CalendarOccurrence(OccurrenceKey.single("calendar", event_id), start, end, **metadata)


def _raises_value_error(callback):
    try:
        callback()
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_utc_and_dataclass_invariants_reject_ambiguous_values():
    _raises_value_error(lambda: OccurrenceKey.single("", "event"))
    _raises_value_error(lambda: OccurrenceKey.recurring("calendar", "series", NOW.replace(tzinfo=None)))
    _raises_value_error(lambda: CalendarParticipant())
    _raises_value_error(lambda: _event("event", NOW, NOW))
    _raises_value_error(lambda: CalendarMatch(_event("event", NOW, NOW + timedelta(minutes=1)),
                                                timedelta(seconds=-1), timedelta()))


def test_participants_dedupe_and_sort_independently_of_source_order():
    first = [
        {"email": " Z@example.test ", "displayName": " Zed "},
        {"displayName": "Name Only"},
        {"email": "a@example.test", "displayName": "Ada"},
        {"displayName": " name   only "},
        {"email": "A@example.test", "displayName": "Ada Lovelace"},
    ]
    second = list(reversed(first))
    expected = (CalendarParticipant(None, "Name Only"),
                CalendarParticipant("a@example.test", "Ada"),
                CalendarParticipant("z@example.test", "Zed"))
    assert normalize_participants(first) == expected
    assert normalize_participants(second) == expected


def test_eligibility_and_explicit_recurrence_key_constructors():
    assert is_event_eligible(all_day=False, status="confirmed", self_response_status=None)
    assert not is_event_eligible(all_day=True, status="confirmed", self_response_status=None)
    assert not is_event_eligible(all_day=False, status="cancelled", self_response_status=None)
    recurring = OccurrenceKey.recurring("calendar", "series", NOW)
    assert recurring == OccurrenceKey("calendar", "series", NOW)
    assert OccurrenceKey.single("calendar", "event").original_start_utc is None
    assert recurring != OccurrenceKey.recurring("calendar", "series", NOW + timedelta(days=7))


def test_matching_uses_overlap_then_start_distance_and_never_private_metadata():
    capture_end = NOW + timedelta(hours=1)
    small = _event("small", NOW, NOW + timedelta(minutes=5), summary="Private")
    large = _event("large", NOW + timedelta(minutes=20), NOW + timedelta(hours=2),
                   participants=(CalendarParticipant("private@example.test"),))
    selected = match_occurrence(NOW, capture_end, [small, large])
    assert selected is not None and selected.occurrence.key.event_id == "large"

    near = _event("near", NOW + timedelta(minutes=2), NOW + timedelta(minutes=20))
    far = _event("far", NOW + timedelta(minutes=5), NOW + timedelta(minutes=20))
    selected = match_occurrence(NOW, NOW + timedelta(minutes=30), [far, near])
    assert selected is not None and selected.occurrence.key.event_id == "near"


def test_matching_handles_grace_ties_and_duplicate_event_rows():
    boundary = _event("boundary", NOW - timedelta(minutes=10), NOW)
    outside = _event("outside", NOW - timedelta(minutes=11), NOW - timedelta(minutes=10, seconds=1))
    assert match_occurrence(NOW, NOW, [boundary]) is not None
    assert match_occurrence(NOW, NOW, [outside]) is None

    left = _event("left", NOW - timedelta(minutes=5), NOW)
    right = _event("right", NOW + timedelta(minutes=5), NOW + timedelta(minutes=6))
    assert match_occurrence(NOW, NOW + timedelta(minutes=1), [left, right]) is None

    duplicate = _event("left", NOW - timedelta(minutes=4), NOW)
    selected = match_occurrence(NOW, NOW + timedelta(minutes=1), [left, duplicate])
    assert selected is not None and selected.occurrence.key == left.key


def test_meeting_snapshot_projects_visible_and_hidden_details():
    key = OccurrenceKey.single("calendar", "event")
    visible = CalendarOccurrence(key, NOW, NOW + timedelta(hours=1), "Visible",
                                 (CalendarParticipant("a@example.test", "Ada"),), True,
                                 " Description ", " Room ", True)
    snapshot = meeting_snapshot(visible)
    assert snapshot.title == "Visible"
    assert snapshot.participant_labels == ("Ada",)
    assert snapshot.description == " Description "
    assert snapshot.location == " Room "
    hidden = meeting_snapshot(CalendarOccurrence(key, NOW, NOW + timedelta(hours=1), "Private",
                                                  (CalendarParticipant("a@example.test", "Ada"),),
                                                  True, "secret", "room", False))
    assert hidden.title is None and hidden.participant_labels == ()
    assert hidden.description is None and hidden.location is None and not hidden.details_visible


def test_meeting_snapshot_metadata_validation_and_selector_roundtrip_are_strict():
    key = OccurrenceKey.recurring("calendar/id", "series", NOW)
    selector = encode_occurrence_selector(key)
    assert decode_occurrence_selector(selector) == key
    for malformed in ("", "!", selector + "x", "e30"):
        _raises_value_error(lambda malformed=malformed: decode_occurrence_selector(malformed))
    _raises_value_error(lambda: MeetingSnapshot(key, "", NOW, NOW + timedelta(hours=1), (), None, None, False))
    _raises_value_error(lambda: MeetingSnapshot(key, None, NOW, NOW, (), None, None, False))
    _raises_value_error(lambda: decode_occurrence_selector("A" * 4097))


def test_meeting_snapshot_visibility_requires_privacy_consistent_payloads():
    key = OccurrenceKey.single("calendar", "event")
    hidden_values = (
        ("Secret", (), None, None),
        (None, ("Alice",), None, None),
        (None, (), "description", None),
        (None, (), None, "location"),
    )
    for title, participants, description, location in hidden_values:
        _raises_value_error(lambda title=title, participants=participants,
                            description=description, location=location:
                            MeetingSnapshot(key, title, NOW, NOW + timedelta(hours=1),
                                            participants, description, location, False))

    visible = MeetingSnapshot(key, "  Review  ", NOW, NOW + timedelta(hours=1),
                              (), None, None, True)
    assert visible.title == "Review"
    hidden = MeetingSnapshot(key, None, NOW, NOW + timedelta(hours=1),
                             (), None, None, False)
    assert hidden.title is None and not hidden.details_visible
