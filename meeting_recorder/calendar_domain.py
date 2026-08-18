"""Immutable Calendar concepts and deterministic offline matching."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import base64
import json
import re
from typing import Iterable, Mapping, Sequence


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

    @classmethod
    def single(cls, calendar_id: str, event_id: str) -> "OccurrenceKey":
        """Construct the identity for one non-recurring Calendar event."""
        return cls(calendar_id, event_id)

    @classmethod
    def recurring(cls, calendar_id: str, series_id: str,
                  original_start_utc: datetime) -> "OccurrenceKey":
        """Construct the identity for one instance of a recurring series."""
        return cls(calendar_id, series_id, original_start_utc)


@dataclass(frozen=True)
class CalendarOccurrence:
    key: OccurrenceKey
    start_utc: datetime
    end_utc: datetime
    summary: str | None = None
    participants: tuple[CalendarParticipant, ...] = ()
    participants_complete: bool | None = None
    description: str | None = None
    location: str | None = None
    details_visible: bool = False

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
        for value, name in ((self.description, "occurrence description"),
                            (self.location, "occurrence location")):
            if value is not None:
                _nonempty(value, name)
        if not isinstance(self.details_visible, bool):
            raise ValueError("occurrence visibility is invalid")


@dataclass(frozen=True)
class CalendarMatch:
    occurrence: CalendarOccurrence
    real_overlap: timedelta
    scheduled_start_distance: timedelta

    def __post_init__(self) -> None:
        if not isinstance(self.occurrence, CalendarOccurrence):
            raise ValueError("match occurrence is invalid")
        if self.real_overlap < timedelta() or self.scheduled_start_distance < timedelta():
            raise ValueError("match scores are invalid")


@dataclass(frozen=True)
class MeetingSnapshot:
    """The safe, recording-facing subset of one Calendar occurrence."""

    occurrence_key: OccurrenceKey
    title: str | None
    scheduled_start_utc: datetime
    scheduled_end_utc: datetime
    participant_labels: tuple[str, ...]
    description: str | None
    location: str | None
    details_visible: bool

    def __post_init__(self) -> None:
        if not isinstance(self.occurrence_key, OccurrenceKey):
            raise ValueError("meeting snapshot key is invalid")
        _utc(self.scheduled_start_utc, "meeting start")
        _utc(self.scheduled_end_utc, "meeting end")
        if self.scheduled_end_utc <= self.scheduled_start_utc:
            raise ValueError("meeting interval is invalid")
        if not isinstance(self.details_visible, bool):
            raise ValueError("meeting visibility is invalid")
        if not self.details_visible:
            if (self.title is not None or self.participant_labels != ()
                    or self.description is not None or self.location is not None):
                raise ValueError("hidden meeting details must be empty")
        else:
            if not isinstance(self.title, str) or not self.title.strip():
                raise ValueError("visible meeting title must be non-empty")
            object.__setattr__(self, "title", self.title.strip())
        if self.description is not None:
            _nonempty(self.description, "meeting description")
        if self.location is not None:
            _nonempty(self.location, "meeting location")
        if not isinstance(self.participant_labels, tuple) or any(
                not isinstance(label, str) or not label for label in self.participant_labels):
            raise ValueError("meeting participant labels are invalid")


def is_event_eligible(*, all_day: bool, status: object, self_response_status: object) -> bool:
    """Apply only explicit eligibility rules, preserving transparent meetings."""
    return not all_day and status in {"confirmed", "tentative"} and self_response_status != "declined"


def normalize_participants(values: Iterable[CalendarParticipant | Mapping[str, object]]) -> tuple[CalendarParticipant, ...]:
    """Normalize explicit people while retaining name-only attendees deterministically."""
    by_identity: dict[str, CalendarParticipant] = {}
    for value in values:
        if isinstance(value, CalendarParticipant):
            email, name = value.email, value.display_name
        elif isinstance(value, Mapping):
            email = value.get("email")
            name = value.get("displayName", value.get("display_name"))
        else:
            continue
        email = email.strip().casefold() if isinstance(email, str) and email.strip() else None
        name = " ".join(name.split()) if isinstance(name, str) and name.split() else None
        if email is None and name is None:
            continue
        participant = CalendarParticipant(email, name)
        identity = f"email:{email}" if email else f"name:{str(name).casefold()}"
        previous = by_identity.get(identity)
        # Keep the first usable explicit name; source ordering must not affect cache output.
        if previous is None or (previous.display_name is None and participant.display_name is not None) or (
                previous.display_name is not None and participant.display_name is not None and
                _participant_order(participant) < _participant_order(previous)):
            by_identity[identity] = participant

    # Sort after dedupe so equivalent attendee order produces one stable cache payload.
    return tuple(sorted(by_identity.values(), key=_participant_order))


def _participant_order(participant: CalendarParticipant) -> tuple[str, str, str]:
    """Order normalized people without making private display spelling an identity."""
    return (participant.email or "", (participant.display_name or "").casefold(),
            participant.display_name or "")


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


def normalized_participant_labels(occurrence: CalendarOccurrence) -> tuple[str, ...]:
    """Expose stable labels without inventing identity for sparse attendees."""
    labels: dict[str, str] = {}
    for participant in occurrence.participants:
        label = (participant.display_name.strip() if participant.display_name
                 else participant.email.casefold() if participant.email else "")
        if label:
            labels.setdefault(label.casefold(), label)
    return tuple(sorted(labels.values(), key=str.casefold))


def meeting_snapshot(occurrence: CalendarOccurrence) -> MeetingSnapshot:
    """Project visible Calendar details while retaining identity and schedule privately."""
    visible = occurrence.details_visible and bool(occurrence.summary and occurrence.summary.strip())
    if not visible:
        return MeetingSnapshot(occurrence.key, None, occurrence.start_utc, occurrence.end_utc,
                               (), None, None, False)
    return MeetingSnapshot(
        occurrence.key, occurrence.summary.strip() if occurrence.summary else None,
        occurrence.start_utc, occurrence.end_utc, normalized_participant_labels(occurrence),
        occurrence.description, occurrence.location, True)


_SELECTOR_VERSION = 1
_SELECTOR_MAX_BYTES = 4096
_SELECTOR_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def encode_occurrence_selector(key: OccurrenceKey) -> str:
    """Encode a complete occurrence key as strict canonical URL-safe JSON."""
    if not isinstance(key, OccurrenceKey):
        raise ValueError("occurrence selector key is invalid")
    payload = {"event_id": key.event_id, "calendar_id": key.calendar_id,
               "original_start_utc": _timestamp(key.original_start_utc)}
    raw = json.dumps({"v": _SELECTOR_VERSION, **payload}, sort_keys=True,
                     separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    if len(encoded) > _SELECTOR_MAX_BYTES:
        raise ValueError("occurrence selector is too large")
    return encoded


def decode_occurrence_selector(value: object) -> OccurrenceKey:
    """Decode only canonical, bounded selectors with an unambiguous key shape."""
    if not isinstance(value, str) or not value or len(value) > _SELECTOR_MAX_BYTES or not _SELECTOR_KEY_RE.fullmatch(value):
        raise ValueError("occurrence selector is malformed")
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("occurrence selector is malformed") from exc
    if not isinstance(data, dict) or set(data) != {"v", "calendar_id", "event_id", "original_start_utc"}:
        raise ValueError("occurrence selector is malformed")
    if data["v"] != _SELECTOR_VERSION or not all(isinstance(data[key], str) for key in ("calendar_id", "event_id")):
        raise ValueError("occurrence selector is malformed")
    original = _parse_timestamp(data["original_start_utc"])
    key = OccurrenceKey(data["calendar_id"], data["event_id"], original)
    if encode_occurrence_selector(key) != value:
        raise ValueError("occurrence selector is not canonical")
    return key


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z") if value else None


def _parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("selector timestamp is malformed")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("selector timestamp is malformed") from exc
    return _utc(parsed, "selector timestamp")
