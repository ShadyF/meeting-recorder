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
    fsync_recording_directory,
    move_regular_file_no_replace,
    MoveCommittedError,
    MovePrecommitError,
    recording_directory_lock,
    visible_recording_path,
)
from .speakr_domain import Tag
from .utils import LOG


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("recording timestamp must be UTC")
    return value


OccurrenceSource = (Callable[[], Sequence[CalendarOccurrence]] |
                    Sequence[CalendarOccurrence])
RenameCallback = Callable[[Path, Path], None]


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
                  started: datetime, ended: datetime, tags: tuple[Tag, ...] = ()) -> MeetingSidecar:
    return MeetingSidecar(media.name, fallback, _utc(started), _utc(ended), meeting, tags)


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


def _notify_media_rename(callback: RenameCallback | None, old_path: Path, new_path: Path) -> None:
    """Notify optional publication tracking only after a committed media move."""
    if callback is None or old_path == new_path:
        return
    try:
        callback(old_path, new_path)
    except Exception as exc:
        # Publication tracking is advisory and must not change enrichment success.
        LOG.warning("Publication rename tracking failed: %s", type(exc).__name__)


def _ensure_regular_media(path: Path) -> None:
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError("recording media must be a regular non-symlink file")


class RecordingEnricher:
    """Synchronously enrich finalized media from a cache-only occurrence provider."""

    def __init__(self, occurrence_provider: OccurrenceSource,
                 to_local: Callable[[datetime], datetime] | None = None,
                 on_media_renamed: RenameCallback | None = None) -> None:
        self.occurrence_provider = occurrence_provider
        self.to_local = to_local or _to_local_default
        self.on_media_renamed = on_media_renamed

    def enrich(self, completed: CompletedRecording, tags: tuple[Tag, ...] | None = None) -> CompletedRecording:
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
            source_metadata = sidecar_path(source)
            original_metadata = None
            if os.path.lexists(source_metadata):
                try:
                    original_metadata = load_sidecar(source_metadata)
                except (OSError, ValueError) as exc:
                    _safe_enrichment_error(exc)
                    return completed
            # Preserve existing tags unless a confirmed selection was supplied.
            selected_tags = original_metadata.tags if tags is None and original_metadata is not None else (tags or ())
            intent = _sidecar_for(destination, fallback, snapshot,
                                  completed.capture_started_at, completed.capture_ended_at, selected_tags)
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
            except MoveCommittedError as exc:
                _safe_enrichment_error(exc)
                return _replace_completed(completed, exc.destination, snapshot)
            except MovePrecommitError as exc:
                _safe_enrichment_error(exc)
                try:
                    if original_metadata is None:
                        remove_sidecar(source_metadata)
                    else:
                        write_sidecar(source_metadata, original_metadata)
                except Exception as repair_error:
                    _safe_enrichment_error(repair_error)
                return _replace_completed(completed, source, snapshot)
            except Exception as exc:
                _safe_enrichment_error(exc)
                try:
                    if original_metadata is None:
                        remove_sidecar(source_metadata)
                    else:
                        write_sidecar(source_metadata, original_metadata)
                except Exception as repair_error:
                    _safe_enrichment_error(repair_error)
                return _replace_completed(completed, source, snapshot)
            destination_metadata = sidecar_path(destination)
            try:
                _write_moved_sidecar(source_metadata, destination_metadata, intent)
            except Exception as exc:
                _safe_enrichment_error(exc)
                return _replace_completed(completed, destination, snapshot)
            _notify_media_rename(self.on_media_renamed, source, destination)
            return _replace_completed(completed, destination, snapshot)


@dataclass(frozen=True)
class CorrectionOutcome:
    """Sanitized state returned with every failed correction transaction."""

    operation: str
    current_path: Path
    success: bool
    committed: bool
    partial: bool
    error_code: str


class CorrectionTransactionError(ValueError):
    """A correction failed and carries the only safe path/result to report."""

    def __init__(self, outcome: CorrectionOutcome) -> None:
        super().__init__(outcome.error_code)
        self.outcome = outcome


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
                 to_local: Callable[[datetime], datetime] | None = None,
                 on_media_renamed: RenameCallback | None = None) -> None:
        self.occurrence_provider = occurrence_provider
        self.to_local = to_local or _to_local_default
        self.on_media_renamed = on_media_renamed

    def _notify_media_renamed(self, old_path: Path, new_path: Path) -> None:
        _notify_media_rename(self.on_media_renamed, old_path, new_path)

    def _recover_duplicate_unlocked(
            self, source: Path, sidecar_file: Path, sidecar: MeetingSidecar,
            intended: Path) -> tuple[Path, Path, MeetingSidecar]:
        """Finish a link-before-unlink crash only when both names share one inode."""
        source_exists = os.path.lexists(source)
        intended_exists = os.path.lexists(intended)
        if source_exists:
            _ensure_regular_media(source)
        if intended_exists:
            _ensure_regular_media(intended)
        if source_exists and intended_exists:
            source_info = os.stat(source, follow_symlinks=False)
            intended_info = os.stat(intended, follow_symlinks=False)
            if (source_info.st_dev, source_info.st_ino) != (intended_info.st_dev, intended_info.st_ino):
                raise ValueError("duplicate recording names refer to different media")
            os.unlink(source)
            try:
                fsync_recording_directory(source.parent)
            except Exception as exc:
                raise CorrectionTransactionError(CorrectionOutcome(
                    "recover", intended, False, True, True, "media-directory-sync-failed")) from exc
        elif source_exists:
            # The destination was not published; the intent remains recoverable.
            return source, sidecar_file, sidecar
        elif not intended_exists:
            raise FileNotFoundError(intended)

        destination_sidecar = sidecar_path(intended)
        if sidecar_file != destination_sidecar:
            try:
                if os.path.lexists(destination_sidecar):
                    existing = load_sidecar(destination_sidecar)
                    if existing.recording_filename != intended.name:
                        raise ValueError("destination sidecar does not describe intended media")
                    remove_sidecar(sidecar_file)
                else:
                    move_regular_file_no_replace(sidecar_file, destination_sidecar)
            except Exception as exc:
                raise CorrectionTransactionError(CorrectionOutcome(
                    "recover", intended, False, True, True, "sidecar-relocation-failed")) from exc
        recovered = load_sidecar(destination_sidecar)
        if recovered.recording_filename != intended.name:
            raise ValueError("recovered sidecar does not describe intended media")
        return intended, destination_sidecar, recovered

    def _discover_unlocked(self, media: Path) -> tuple[Path, Path, MeetingSidecar] | None:
        _ensure_regular_media(media)
        direct = sidecar_path(media)
        if os.path.lexists(direct):
            sidecar = load_sidecar(direct)
            if (sidecar.recording_filename != media.name
                    and sidecar.original_fallback_filename != media.name):
                raise ValueError("direct sidecar does not describe this media")
            if sidecar.recording_filename != media.name:
                recovered = self._recover_duplicate_unlocked(
                    media, direct, sidecar, media.with_name(sidecar.recording_filename))
                self._notify_media_renamed(media, recovered[0])
                return recovered
            return media, direct, sidecar
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
        if not candidates:
            return None
        candidate, sidecar = candidates[0]
        old_name = candidate.name[:-len(".meeting.json")]
        old_media = media.with_name(old_name)
        recovery_source = media if old_media == media else old_media
        recovered = self._recover_duplicate_unlocked(
            recovery_source, candidate, sidecar, media,
        )
        self._notify_media_renamed(recovery_source, recovered[0])
        return recovered

    def discover(self, media: Path | str) -> MeetingSidecar | None:
        media_path = Path(media)
        with recording_directory_lock(media_path.parent):
            found = self._discover_unlocked(media_path)
            return found[2] if found else None

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
            _, _, sidecar = found
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
            media_path, sidecar_file, sidecar = found
            try:
                fallback = collision_safe_path(
                    media_path.with_name(sidecar.original_fallback_filename), media_path)
            except Exception as exc:
                raise self._failure("clear", media_path, False, False,
                                    "collision-selection-failed", exc) from exc
            intent = replace(sidecar, recording_filename=fallback.name, meeting=None)
            try:
                self._commit_clear(media_path, sidecar_file, sidecar, fallback, intent)
            except CorrectionTransactionError:
                raise
            except Exception as exc:
                raise self._failure("clear", media_path, False, False,
                                    "precommit-failed", exc) from exc
            self._notify_media_renamed(media_path, fallback)
            return fallback

    def _change(self, media: Path | str, key: OccurrenceKey,
                supplied: Iterable[CalendarOccurrence] | None) -> Path:
        media_path = Path(media)
        with recording_directory_lock(media_path.parent):
            found = self._discover_unlocked(media_path)
            if found is None:
                raise ValueError("recording sidecar is missing")
            media_path, sidecar_file, sidecar = found
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
                raise self._failure("select", media_path, False, False,
                                    "collision-selection-failed", exc) from exc
            intent = replace(sidecar, recording_filename=destination.name,
                             meeting=_meeting_snapshot(occurrence))
            try:
                self._commit_select(media_path, sidecar_file, sidecar, destination, intent)
            except CorrectionTransactionError:
                raise
            except Exception as exc:
                raise self._failure("select", media_path, False, False,
                                    "precommit-failed", exc) from exc
            self._notify_media_renamed(media_path, destination)
            return destination

    @staticmethod
    def _failure(operation: str, current_path: Path, committed: bool,
                 partial: bool, error_code: str, cause: Exception) -> CorrectionTransactionError:
        _safe_enrichment_error(cause)
        return CorrectionTransactionError(CorrectionOutcome(
            operation, current_path, False, committed, partial, error_code))

    @staticmethod
    def _restore_exact(sidecar_file: Path, original: MeetingSidecar) -> None:
        write_sidecar(sidecar_file, original)

    def _verify_select(self, source: Path, destination: Path,
                       destination_sidecar: Path, intent: MeetingSidecar) -> None:
        _ensure_regular_media(destination)
        if source != destination and os.path.lexists(source):
            raise ValueError("source media still exists after selection")
        actual = load_sidecar(destination_sidecar)
        if actual != intent:
            raise ValueError("selection sidecar was not committed")

    def _commit_select(self, source: Path, sidecar_file: Path,
                       original: MeetingSidecar, destination: Path,
                       intent: MeetingSidecar) -> None:
        moved = False
        try:
            write_sidecar(sidecar_file, intent)
            if destination != source:
                move_regular_file_no_replace(source, destination)
                moved = True
                try:
                    _write_moved_sidecar(sidecar_file, sidecar_path(destination), intent)
                except Exception as exc:
                    raise self._failure("select", destination, True, True,
                                        "sidecar-relocation-failed", exc) from exc
            self._verify_select(source, destination, sidecar_path(destination), intent)
        except MoveCommittedError as exc:
            raise self._failure("select", destination, True, True,
                                "media-directory-sync-failed", exc) from exc
        except CorrectionTransactionError:
            raise
        except Exception as exc:
            if moved:
                raise self._failure("select", destination, True, True,
                                    "postcommit-failed", exc) from exc
            try:
                self._restore_exact(sidecar_file, original)
            except Exception as repair_error:
                _safe_enrichment_error(repair_error)
            raise self._failure("select", source, False, False,
                                "precommit-failed", exc) from exc

    def _commit_clear(self, source: Path, sidecar_file: Path,
                       original: MeetingSidecar, destination: Path,
                       intent: MeetingSidecar) -> None:
        moved = False
        try:
            # Persist the metadata-free intent before changing the media name.
            write_sidecar(sidecar_file, intent)

            # Publish the collision-safe fallback media before relocating its sidecar.
            if destination != source:
                move_regular_file_no_replace(source, destination)
                moved = True

            # Retain tagged v2 metadata while removing only its Meeting snapshot.
            if intent.tags:
                _write_moved_sidecar(sidecar_file, sidecar_path(destination), intent)
                actual = load_sidecar(sidecar_path(destination))
                if actual != intent:
                    raise ValueError("clear sidecar was not committed")
            else:
                # Keep legacy clears sidecar-free once the media move has succeeded.
                try:
                    remove_sidecar(sidecar_file)
                except Exception as exc:
                    if destination == source:
                        try:
                            self._restore_exact(sidecar_file, original)
                        except Exception as repair_error:
                            _safe_enrichment_error(repair_error)
                        raise self._failure("clear", source, False, False,
                                            "sidecar-removal-failed", exc) from exc
                    raise self._failure("clear", destination, True, True,
                                        "sidecar-removal-failed", exc) from exc

                # Reject a remaining legacy sidecar after its explicit removal.
                if os.path.lexists(sidecar_path(destination)):
                    raise ValueError("clear sidecar still exists")

            # Verify the media remains authoritative for either clear representation.
            _ensure_regular_media(destination)
        except MoveCommittedError as exc:
            raise self._failure("clear", destination, True, True,
                                "media-directory-sync-failed", exc) from exc
        except CorrectionTransactionError:
            raise
        except Exception as exc:
            if not moved:
                try:
                    self._restore_exact(sidecar_file, original)
                except Exception as repair_error:
                    _safe_enrichment_error(repair_error)
                raise self._failure("clear", source, False, False,
                                    "precommit-failed", exc) from exc
            raise self._failure("clear", destination, True, True,
                                "postcommit-failed", exc) from exc
