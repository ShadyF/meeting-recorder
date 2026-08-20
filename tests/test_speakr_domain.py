"""Speakr publication identity, state, and metadata projection tests."""

from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from meeting_recorder.calendar_domain import (
    CalendarOccurrence, CalendarParticipant, OccurrenceKey, meeting_snapshot,
)
from meeting_recorder.meeting_sidecar import MeetingSidecar
from meeting_recorder.speakr_domain import (
    MediaIdentity, PublicationJob, PublicationKey, PublicationResult,
    PublicationState, SpeakrMetadata, map_speakr_metadata, normalize_speakr_url,
)


NOW = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
HASH = "a" * 64


def _raises(callable_object, *args, **kwargs) -> None:
    try:
        callable_object(*args, **kwargs)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def _meeting(*, visible=True, title="  Design review  ", description=None, location=None,
             participants=()):
    return meeting_snapshot(CalendarOccurrence(
        OccurrenceKey.single("calendar", "event"), NOW, NOW + timedelta(hours=1),
        summary=title if visible else None, description=description, location=location,
        participants=tuple(CalendarParticipant(display_name=label) for label in participants),
        details_visible=visible,
    ))


def _sidecar(meeting=None) -> MeetingSidecar:
    return MeetingSidecar("renamed.mkv", "original.mkv", NOW, NOW + timedelta(minutes=1), meeting)


def _media() -> MediaIdentity:
    return MediaIdentity(Path("renamed.mkv"), 1, 2, 3, 4_000_000_000)


def _job(state=PublicationState.READY, **changes) -> PublicationJob:
    values: dict[str, Any] = dict(
        key=PublicationKey("HTTPS://EXAMPLE.COM:443/", HASH.upper()),
        media=_media(),
        metadata=SpeakrMetadata("Title", NOW, "", ""),
        state=state,
        attempt_count=1,
    )
    if state is PublicationState.TRANSFERRING:
        values["transfer_started_at"] = NOW
    elif state in {PublicationState.TRANSFER_REJECTED, PublicationState.TRANSFER_UNKNOWN}:
        values.update(transfer_started_at=NOW, transfer_completed_at=NOW + timedelta(seconds=1),
                      error_code="network_error", http_status=503)
    elif state is PublicationState.METADATA_PENDING:
        values.update(remote_recording_id=9, transfer_started_at=NOW,
                      transfer_completed_at=NOW + timedelta(seconds=1))
    elif state is PublicationState.PUBLISHED:
        values.update(remote_recording_id=9, transfer_started_at=NOW,
                      transfer_completed_at=NOW + timedelta(seconds=1),
                      published_at=NOW + timedelta(seconds=2))
    values.update(changes)
    return PublicationJob(**values)


def test_url_normalization_canonicalizes_origins_and_ports() -> None:
    cases = {
        "http://EXAMPLE.com/": "http://example.com",
        "https://EXAMPLE.com:443": "https://example.com",
        "http://192.0.2.10:8080/": "http://192.0.2.10:8080",
        "https://[2001:DB8::1]/": "https://[2001:db8::1]",
        "http://[2001:DB8::2]:8080/": "http://[2001:db8::2]:8080",
        "https://example.com:8443": "https://example.com:8443",
    }
    for source, expected in cases.items():
        assert normalize_speakr_url(source) == expected


def test_url_normalization_rejects_every_non_origin_class() -> None:
    rejected = (
        "", "ftp://example.com", "https:///missing-host", "https://",
        "https://user@example.com", "https://user:secret@example.com",
        "https://example.com?token=secret", "https://example.com#secret",
        "https://example.com/path", "https://example.com//",
        "https://example.com:", "https://example.com:abc", "https://example.com:65536",
        "https://example.com:-1", "https://[not-ipv6]", "https://[2001:db8::1",
        "https://example.com\n", "https://example.com\t", "https://example.com\u2003",
        "https://token@example.com/path?x=1#y",
    )
    for value in rejected:
        _raises(normalize_speakr_url, value)


def test_keys_and_media_identity_are_canonical_and_nonnegative() -> None:
    key = PublicationKey("HTTP://Example.com:80/", HASH.upper())
    assert key.instance_url == "http://example.com"
    assert key.recording_sha256 == HASH
    for field in ("device", "inode", "size", "mtime_ns"):
        _raises(replace, _media(), **{field: -1})
    _raises(PublicationKey, "https://example.com", "g" * 64)
    _raises(PublicationKey, "https://example.com", "a" * 63)
    _raises(PublicationKey, "https://example.com", True)


def test_publication_states_and_results_enforce_frozen_state_machine() -> None:
    assert [state.value for state in PublicationState] == [
        "ready", "transferring", "transfer_rejected", "transfer_unknown",
        "metadata_pending", "published",
    ]
    assert _job().state is PublicationState.READY
    assert _job(PublicationState.TRANSFERRING).transfer_started_at == NOW
    assert _job(PublicationState.PUBLISHED).remote_recording_id == 9
    assert PublicationResult(_job(PublicationState.PUBLISHED), True).already_published
    _raises(PublicationResult, _job(), 1)
    _raises(_job, PublicationState.METADATA_PENDING, remote_recording_id=True)
    _raises(_job, PublicationState.PUBLISHED, remote_recording_id=0)
    _raises(_job, PublicationState.READY, transfer_started_at=NOW)
    _raises(_job, PublicationState.PUBLISHED, published_at=None)
    _raises(_job, PublicationState.TRANSFER_REJECTED, error_code="Bearer secret")
    _raises(_job, PublicationState.TRANSFER_REJECTED, http_status=True)
    _raises(_job, PublicationState.METADATA_PENDING, error_code="network_error")


def test_publication_job_has_only_public_schema_fields_and_is_immutable() -> None:
    job = _job(PublicationState.PUBLISHED)
    names = {field.name for field in fields(PublicationJob)}
    assert not names.intersection({"token", "credentials", "authorization", "headers", "request_metadata"})
    assert "secret" not in repr(job).casefold()
    assert "secret" not in repr(PublicationKey("https://example.com", HASH)).casefold()
    _raises(_job, PublicationState.TRANSFER_UNKNOWN, error_code="x\n")
    try:
        job.state = PublicationState.READY
        assert False, "publication jobs must be frozen"
    except AttributeError:
        pass


def test_unmatched_and_hidden_sidecars_use_current_media_fallbacks() -> None:
    with TemporaryDirectory() as directory:
        media = Path(directory) / "renamed.capture.mkv"
        mtime_ns = 1_735_689_123_456_789_000
        for sidecar in (None, _sidecar(), _sidecar(_meeting(visible=False))):
            result = map_speakr_metadata(media, mtime_ns, sidecar)
            assert result.title == "renamed.capture"
            assert result.meeting_date == datetime.fromtimestamp(mtime_ns / 1e9, timezone.utc)
            assert result.notes == ""
            assert result.participants == ""


def test_visible_match_maps_public_fields_and_preserves_description_lines() -> None:
    meeting = _meeting(
        description="  First line  \r\nSecond\tline  ", location="  Room 7 ",
        participants=(" Alice ", "Bob\tSmith"),
    )
    result = map_speakr_metadata(Path("current-name.mkv"), 4_000_000_000, _sidecar(meeting))
    assert result.title == "Design review"
    assert result.meeting_date == NOW
    assert result.notes == "First line\nSecond line\n\nLocation: Room 7"
    assert result.participants == "Alice, Bob Smith"


def test_visible_match_supports_description_only_location_only_and_public_cleaning() -> None:
    for description, location, expected in (
        ("Details", None, "Details"),
        (None, "Room", "Location: Room"),
        ("Details", "Room", "Details\n\nLocation: Room"),
    ):
        result = map_speakr_metadata(Path("current.mkv"), 0,
                                     _sidecar(_meeting(description=description, location=location)))
        assert result.notes == expected
    cleaned = map_speakr_metadata(
        Path("  current.mkv"), 0,
        _sidecar(_meeting(description="a\x00 b", participants=("\tAlice\n",))),
    )
    assert cleaned.notes == "a b"
    assert cleaned.participants == "Alice"


def test_metadata_requires_utc_dates_and_public_strings() -> None:
    _raises(SpeakrMetadata, "Title", datetime.now(), "", "")
    _raises(SpeakrMetadata, "\x00", NOW, "", "")
    _raises(SpeakrMetadata, "Title", NOW, "", ["Alice"])
