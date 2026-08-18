"""Offline Calendar enrichment and correction transactions for recordings."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .calendar_domain import (
    CalendarOccurrence,
    MeetingSnapshot,
    OccurrenceKey,
    match_occurrence,
    meeting_snapshot,
)
from .domain import CompletedRecording
from .meeting_sidecar import MeetingSidecar, load_sidecar, remove_sidecar, sidecar_path, write_sidecar
from .recording_paths import (
    collision_safe_path,
    move_regular_file_no_replace,
    recording_directory_lock,
    visible_recording_path,
)
from .utils import LOG


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("recording timestamp must be UTC")
    return value


OccurrenceSource = (Callable[[], Sequence[CalendarOccurrence]] |
                    Sequence[CalendarOccurrence])


def _provider_occurrences(provider: OccurrenceSource) -> tuple[CalendarOccurrence, ...]:
    """Read only already-cached occurrences, never triggering refresh/network work."""
    if provider is None:
        return ()
    try:
        values = provider() if callable(provider) else provider
        result = []
        for item in values:
            if not isinstance(item, CalendarOccurrence):
                raise ValueError("occurrence provider returned an invalid item")
            result.append(item)
        return tuple(result)
    except Exception as exc:
        LOG.warning("Calendar cache unavailable for recording enrichment: %s",
                    type(exc).__name__)
        return ()


def cache_only_occurrence_provider() -> Callable[[], Sequence[CalendarOccurrence]]:
    """Build a provider that reads only fresh selected Calendar cache entries."""
    def load() -> Sequence[CalendarOccurrence]:
        try:
            from .calendar_cache import CalendarCache
            from .config import load_raw_config, validate_google_calendar_ids

            raw = load_raw_config()
            selected = validate_google_calendar_ids(raw.get("google_calendar_ids", []))
            return CalendarCache().load_selected_occurrences(
                selected, datetime.now(timezone.utc))
        except Exception as exc:
            LOG.warning("Calendar cache unavailable for recording enrichment: %s",
                        type(exc).__name__)
            return ()

    return load


def _to_local_default(value: datetime) -> datetime:
    return value.astimezone()


def _meeting_snapshot(occurrence: CalendarOccurrence | None) -> MeetingSnapshot | None:
    return meeting_snapshot(occurrence) if occurrence is not None else None


def _replace_completed(completed: CompletedRecording, path: Path,
                       meeting: MeetingSnapshot | None) -> CompletedRecording:
    return replace(completed, path=path, meeting=meeting)


def _existing_fallback(media: Path) -> str:
    metadata = sidecar_path(media)
    if not os.path.lexists(metadata):
        return media.name
    current = load_sidecar(metadata)
    if current.recording_filename != media.name:
        raise ValueError("existing sidecar does not describe this media")
    return current.original_fallback_filename


def _sidecar_for(media: Path, fallback: str, meeting: MeetingSnapshot | None,
                 started: datetime, ended: datetime) -> MeetingSidecar:
    return MeetingSidecar(media.name, fallback, _utc(started), _utc(ended), meeting)


def _visible_meeting(occurrence: CalendarOccurrence) -> bool:
    return bool(occurrence.details_visible and occurrence.summary and occurrence.summary.strip())


def _write_moved_sidecar(source_sidecar: Path, destination_sidecar: Path,
                         sidecar: MeetingSidecar) -> None:
    """Relocate metadata whose filename intent was published before the media move."""
    if source_sidecar == destination_sidecar:
        write_sidecar(destination_sidecar, sidecar)
        return
    move_regular_file_no_replace(source_sidecar, destination_sidecar)


def _safe_enrichment_error(exc: Exception) -> None:
    # Keep filesystem details out of logs while retaining a useful failure class.
    LOG.warning("Recording metadata transaction failed: %s", type(exc).__name__)


def _ensure_regular_media(path: Path) -> None:
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError("recording media must be a regular non-symlink file")


class RecordingEnricher:
    """Synchronously enrich finalized media from a cache-only occurrence provider."""

    def __init__(self, occurrence_provider: OccurrenceSource,
                 to_local: Callable[[datetime], datetime] | None = None) -> None:
        self.occurrence_provider = occurrence_provider
        self.to_local = to_local or _to_local_default

    def enrich(self, completed: CompletedRecording) -> CompletedRecording:
        """Write unmatched metadata or transactionally move a uniquely matched recording."""
        source = Path(completed.path)
        with recording_directory_lock(source.parent):
            try:
                _ensure_regular_media(source)
            except (OSError, ValueError) as exc:
                _safe_enrichment_error(exc)
                return completed
            occurrences = _provider_occurrences(self.occurrence_provider)
            try:
                match = match_occurrence(completed.capture_started_at,
                                         completed.capture_ended_at, occurrences)
            except Exception as exc:
                _safe_enrichment_error(exc)
                match = None
            meeting = match.occurrence if match is not None else None
            try:
                fallback = _existing_fallback(source)
            except (OSError, ValueError) as exc:
                _safe_enrichment_error(exc)
                return completed
            destination = source
            if meeting is not None and _visible_meeting(meeting):
                try:
                    preferred = visible_recording_path(source.parent, meeting, source.suffix, self.to_local)
                    destination = collision_safe_path(preferred, source)
                except Exception as exc:
                    _safe_enrichment_error(exc)
                    destination = source
            snapshot = _meeting_snapshot(meeting)
            intent = _sidecar_for(destination, fallback, snapshot,
                                  completed.capture_started_at, completed.capture_ended_at)
            source_metadata = sidecar_path(source)
            try:
                # Publish intent beside the authoritative media before changing its name.
                write_sidecar(source_metadata, intent)
            except Exception as exc:
                _safe_enrichment_error(exc)
                return completed
            if destination == source:
                return _replace_completed(completed, source, snapshot)
            try:
                move_regular_file_no_replace(source, destination)
            except Exception as exc:
                _safe_enrichment_error(exc)
                try:
                    write_sidecar(source_metadata, replace(intent, recording_filename=source.name))
                except Exception as repair_error:
                    _safe_enrichment_error(repair_error)
                return _replace_completed(completed, source, snapshot)
            destination_metadata = sidecar_path(destination)
            try:
                _write_moved_sidecar(source_metadata, destination_metadata, intent)
            except Exception as exc:
                _safe_enrichment_error(exc)
                return _replace_completed(completed, destination, snapshot)
            return _replace_completed(completed, destination, snapshot)


@dataclass(frozen=True)
class NearbyOccurrence:
    occurrence: CalendarOccurrence
    interval_distance: timedelta
    scheduled_start_distance: timedelta
    is_current: bool

    @property
    def key(self) -> OccurrenceKey:
        return self.occurrence.key

    @property
    def selector(self) -> str:
        from .calendar_domain import encode_occurrence_selector
        return encode_occurrence_selector(self.occurrence.key)

class RecordingCorrectionService:
    """Correct sidecars and names from supplied cached Calendar occurrences only."""

    def __init__(self, occurrence_provider: OccurrenceSource = (),
                 to_local: Callable[[datetime], datetime] | None = None) -> None:
        self.occurrence_provider = occurrence_provider
        self.to_local = to_local or _to_local_default

    def _discover_unlocked(self, media: Path) -> tuple[Path, MeetingSidecar] | None:
        _ensure_regular_media(media)
        direct = sidecar_path(media)
        if os.path.lexists(direct):
            sidecar = load_sidecar(direct)
            if (sidecar.recording_filename != media.name
                    and sidecar.original_fallback_filename != media.name):
                raise ValueError("direct sidecar does not describe this media")
            return direct, sidecar
        candidates: list[tuple[Path, MeetingSidecar]] = []
        for candidate in sorted(media.parent.glob(f"*{'.meeting.json'}"), key=lambda item: item.name):
            try:
                sidecar = load_sidecar(candidate)
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                # An unrelated crash artifact must not hide one validated match.
                continue
            if sidecar.recording_filename == media.name:
                candidates.append((candidate, sidecar))
        if len(candidates) > 1:
            raise ValueError("multiple sidecars describe this media")
        return candidates[0] if candidates else None

    def discover(self, media: Path | str) -> MeetingSidecar | None:
        media_path = Path(media)
        with recording_directory_lock(media_path.parent):
            found = self._discover_unlocked(media_path)
            return found[1] if found else None

    def _occurrences(self, supplied: Iterable[CalendarOccurrence] | None) -> tuple[CalendarOccurrence, ...]:
        return tuple(supplied) if supplied is not None else _provider_occurrences(self.occurrence_provider)

    def list_nearby(self, media: Path | str,
                    occurrences: Iterable[CalendarOccurrence] | None = None
                    ) -> tuple[NearbyOccurrence, ...]:
        media_path = Path(media)
        with recording_directory_lock(media_path.parent):
            found = self._discover_unlocked(media_path)
            if found is None:
                return ()
            _, sidecar = found
            capture_start, capture_end = sidecar.capture_started_at, sidecar.capture_ended_at
            current_key = sidecar.meeting.occurrence_key if sidecar.meeting else None
            nearby = []
            for occurrence in self._occurrences(occurrences):
                if occurrence.end_utc < capture_start - timedelta(hours=24):
                    continue
                if occurrence.start_utc > capture_end + timedelta(hours=24):
                    continue
                distance = max(timedelta(), occurrence.start_utc - capture_end,
                                capture_start - occurrence.end_utc)
                nearby.append(NearbyOccurrence(
                    occurrence, distance, abs(occurrence.start_utc - capture_start),
                                               occurrence.key == current_key))
            return tuple(sorted(nearby, key=lambda item: (
                item.interval_distance, item.scheduled_start_distance,
                item.occurrence.start_utc,
                item.key.calendar_id, item.key.event_id,
                item.key.original_start_utc or datetime.min.replace(tzinfo=timezone.utc))))

    def select(self, media: Path | str, key: OccurrenceKey | str,
               occurrences: Iterable[CalendarOccurrence] | None = None) -> Path:
        if isinstance(key, str):
            from .calendar_domain import decode_occurrence_selector
            key = decode_occurrence_selector(key)
        return self._change(media, key, occurrences)

    def clear(self, media: Path | str) -> Path:
        media_path = Path(media)
        with recording_directory_lock(media_path.parent):
            found = self._discover_unlocked(media_path)
            if found is None:
                return media_path
            sidecar_file, sidecar = found
            try:
                fallback = collision_safe_path(
                    media_path.with_name(sidecar.original_fallback_filename), media_path)
            except Exception as exc:
                _safe_enrichment_error(exc)
                return media_path
            intent = replace(sidecar, recording_filename=fallback.name, meeting=None)
            moved = False
            try:
                write_sidecar(sidecar_file, intent)
                if fallback != media_path:
                    move_regular_file_no_replace(media_path, fallback)
                    moved = True
                    remove_sidecar(sidecar_file)
                else:
                    remove_sidecar(sidecar_file)
                return fallback
            except Exception as exc:
                _safe_enrichment_error(exc)
                if not moved:
                    try:
                        write_sidecar(sidecar_file, replace(intent, recording_filename=media_path.name))
                    except Exception as repair_error:
                        _safe_enrichment_error(repair_error)
                return fallback if moved else media_path

    def _change(self, media: Path | str, key: OccurrenceKey,
                supplied: Iterable[CalendarOccurrence] | None) -> Path:
        media_path = Path(media)
        with recording_directory_lock(media_path.parent):
            found = self._discover_unlocked(media_path)
            if found is None:
                raise ValueError("recording sidecar is missing")
            sidecar_file, sidecar = found
            matches = [item for item in self._occurrences(supplied) if item.key == key]
            if len(matches) != 1:
                raise ValueError("selected occurrence is not in the supplied cache")
            occurrence = matches[0]
            try:
                if _visible_meeting(occurrence):
                    preferred = visible_recording_path(media_path.parent, occurrence,
                                                       media_path.suffix, self.to_local)
                    destination = collision_safe_path(preferred, media_path)
                else:
                    destination = collision_safe_path(
                        media_path.with_name(sidecar.original_fallback_filename), media_path)
            except Exception as exc:
                _safe_enrichment_error(exc)
                return media_path
            intent = replace(sidecar, recording_filename=destination.name,
                             meeting=_meeting_snapshot(occurrence))
            moved = False
            try:
                write_sidecar(sidecar_file, intent)
                if destination != media_path:
                    move_regular_file_no_replace(media_path, destination)
                    moved = True
                    _write_moved_sidecar(sidecar_file, sidecar_path(destination), intent)
                return destination
            except Exception as exc:
                _safe_enrichment_error(exc)
                if not moved:
                    try:
                        write_sidecar(sidecar_file, replace(intent, recording_filename=media_path.name))
                    except Exception as repair_error:
                        _safe_enrichment_error(repair_error)
                return destination if moved else media_path
