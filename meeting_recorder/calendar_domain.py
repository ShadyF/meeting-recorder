"""Immutable Calendar concepts and deterministic offline matching."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Sequence


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be non-empty")
    return value


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be UTC")
    return value


@dataclass(frozen=True)
class CalendarInfo:
    id: str
    summary: str | None = None
    primary: bool = False
    access_role: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.id, "calendar id")
        if self.summary is not None:
            _nonempty(self.summary, "calendar summary")
        if self.access_role is not None:
            _nonempty(self.access_role, "calendar access role")


@dataclass(frozen=True)
class CalendarParticipant:
    email: str | None = None
    display_name: str | None = None

    def __post_init__(self) -> None:
        if self.email is None and self.display_name is None:
            raise ValueError("participant requires an email or display name")
        if self.email is not None:
            _nonempty(self.email, "participant email")
        if self.display_name is not None:
            _nonempty(self.display_name, "participant display name")


@dataclass(frozen=True)
class OccurrenceKey:
    calendar_id: str
    event_id: str
    original_start_utc: datetime | None = None

    def __post_init__(self) -> None:
        _nonempty(self.calendar_id, "calendar id")
        _nonempty(self.event_id, "event id")
        if self.original_start_utc is not None:
            _utc(self.original_start_utc, "original start")


@dataclass(frozen=True)
class CalendarOccurrence:
    key: OccurrenceKey
    start_utc: datetime
    end_utc: datetime
    summary: str | None = None
    participants: tuple[CalendarParticipant, ...] = ()
    participants_complete: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, OccurrenceKey):
            raise ValueError("occurrence key is invalid")
        _utc(self.start_utc, "occurrence start")
        _utc(self.end_utc, "occurrence end")
        if self.end_utc <= self.start_utc:
            raise ValueError("occurrence interval is invalid")
        if self.summary is not None:
            _nonempty(self.summary, "occurrence summary")
        if not isinstance(self.participants, tuple) or not all(
                isinstance(item, CalendarParticipant) for item in self.participants):
            raise ValueError("occurrence participants are invalid")
        if self.participants_complete is not None and not isinstance(self.participants_complete, bool):
            raise ValueError("participant completeness is invalid")


@dataclass(frozen=True)
class CalendarMatch:
    occurrence: CalendarOccurrence
    real_overlap: timedelta
    scheduled_start_distance: timedelta


def occurrence_key(calendar_id: str, event_id: str, recurring_event_id: str | None = None,
                   original_start_utc: datetime | None = None) -> OccurrenceKey:
    if recurring_event_id is None:
        return OccurrenceKey(calendar_id, event_id)
    _nonempty(recurring_event_id, "recurring event id")
    if original_start_utc is None:
        raise ValueError("recurrence requires original start")
    return OccurrenceKey(calendar_id, recurring_event_id, original_start_utc)


def is_event_eligible(*, all_day: bool, status: object, self_response_status: object) -> bool:
    """Apply only explicit eligibility rules, preserving transparent meetings."""
    return not all_day and status in {"confirmed", "tentative"} and self_response_status != "declined"


def normalize_participants(values: Iterable[CalendarParticipant | dict[str, object]]) -> tuple[CalendarParticipant, ...]:
    """Normalize explicit people while retaining name-only attendees deterministically."""
    result: list[CalendarParticipant] = []
    indexes: dict[str, int] = {}
    for value in values:
        if isinstance(value, CalendarParticipant):
            email, name = value.email, value.display_name
        elif isinstance(value, dict):
            email = value.get("email")
            name = value.get("displayName", value.get("display_name"))
        else:
            continue
        email = email.strip().casefold() if isinstance(email, str) and email.strip() else None
        name = " ".join(name.split()) if isinstance(name, str) and name.split() else None
        if email is None and name is None:
            continue
        participant = CalendarParticipant(email, name)
        identity = f"email:{email}" if email else f"name:{name.casefold()}"
        prior = indexes.get(identity)
        if prior is None:
            indexes[identity] = len(result)
            result.append(participant)
        elif result[prior].display_name is None and participant.display_name is not None:
            result[prior] = participant
    return tuple(result)


def match_occurrence(capture_start: datetime, capture_end: datetime,
                     occurrences: Sequence[CalendarOccurrence],
                     boundary_grace: timedelta = timedelta(minutes=10)) -> CalendarMatch | None:
    """Select one unique key by greatest real overlap, then scheduled-start distance."""
    _utc(capture_start, "capture start")
    _utc(capture_end, "capture end")
    if capture_end < capture_start or boundary_grace < timedelta(0):
        raise ValueError("capture interval or grace is invalid")
    candidates: dict[OccurrenceKey, CalendarMatch] = {}
    for occurrence in occurrences:
        if not isinstance(occurrence, CalendarOccurrence):
            raise ValueError("occurrence is invalid")
        if occurrence.end_utc < capture_start - boundary_grace or occurrence.start_utc > capture_end + boundary_grace:
            continue
        overlap = max(timedelta(), min(capture_end, occurrence.end_utc) - max(capture_start, occurrence.start_utc))
        candidate = CalendarMatch(occurrence, overlap, abs(occurrence.start_utc - capture_start))
        prior = candidates.get(occurrence.key)
        if prior is None or _score(candidate) < _score(prior):
            candidates[occurrence.key] = candidate
    if not candidates:
        return None
    score = min(_score(item) for item in candidates.values())
    winners = [item for item in candidates.values() if _score(item) == score]
    return winners[0] if len(winners) == 1 else None


def _score(match: CalendarMatch) -> tuple[timedelta, timedelta]:
    return (-match.real_overlap, match.scheduled_start_distance)
