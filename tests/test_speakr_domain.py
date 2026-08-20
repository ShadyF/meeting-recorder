"""Speakr publication identity, state, and metadata projection tests."""

from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    except (TypeError, ValueError):
        return
    raise AssertionError("expected validation failure")


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
    values = dict(
        key=PublicationKey("HTTPS://EXAMPLE.COM:443/", HASH.upper()),
        media_device=1, media_inode=2, media_size=3,
        source_mtime_ns=4_000_000_000, file_last_modified_ms=4_000,
        state=state, created_at_ms=1_000, updated_at_ms=1_000,
    )
    if state is PublicationState.TRANSFERRING:
        values.update(attempt_count=1, transfer_started_at_ms=1_000)
    elif state in {PublicationState.TRANSFER_REJECTED, PublicationState.TRANSFER_UNKNOWN}:
        values.update(attempt_count=1, transfer_started_at_ms=1_000,
                      last_error_code=(
                          "transfer_rejected" if state is PublicationState.TRANSFER_REJECTED
                          else "transfer_unknown"
                      ), last_http_status=503, updated_at_ms=2_000)
    elif state is PublicationState.METADATA_PENDING:
        values.update(attempt_count=1, remote_recording_id=9,
                      transfer_started_at_ms=1_000, accepted_at_ms=2_000, updated_at_ms=2_000)
    elif state is PublicationState.PUBLISHED:
        values.update(attempt_count=1, remote_recording_id=9,
                      transfer_started_at_ms=1_000, accepted_at_ms=2_000,
                      published_at_ms=3_000, updated_at_ms=3_000)
    values.update(changes)
    return PublicationJob(**values)


def test_url_normalization_canonicalizes_origins_and_ports() -> None:
    cases = {
        "http://EXAMPLE.com/": "http://example.com",
        "https://EXAMPLE.com:443": "https://example.com",
        "http://192.0.2.10:8080/": "http://192.0.2.10:8080",
        "https://[2001:DB8::1]/": "https://[2001:db8::1]",
        "http://[2001:DB8::2]:8080/": "http://[2001:db8::2]:8080",
        "https://[2001:0db8:0000:0000:0000:0000:0000:0001]": "https://[2001:db8::1]",
        "https://BÜCHER.example/": "https://xn--bcher-kva.example",
        "https://XN--BCHER-KVA.EXAMPLE": "https://xn--bcher-kva.example",
        "https://example.com:8443": "https://example.com:8443",
    }
    for source, expected in cases.items():
        assert normalize_speakr_url(source) == expected


def test_url_normalization_rejects_every_non_origin_class() -> None:
    rejected = (
        "", "ftp://example.com", "https:///missing-host", "https://",
        "https://user@example.com", "https://user:secret@example.com",
        "https://example.com?token=secret", "https://example.com#secret",
        "https://example.com/path", "https://example.com//", "https://example.com:",
        "https://example.com:abc", "https://example.com:65536", "https://example.com:-1",
        "https://[not-ipv6]", "https://[2001:db8::1", "https://example.com\n",
        "https://example.com\t", "https://example.com\u2003",
        "https://[fe80::1%eth0]", "https://[FE80::1%25eth0]",
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


def test_publication_job_has_only_public_scalar_schema_and_validates_states() -> None:
    assert [state.value for state in PublicationState] == [
        "ready", "transferring", "transfer_rejected", "transfer_unknown",
        "metadata_pending", "published",
    ]
    names = {field.name for field in fields(PublicationJob)}
    assert names == {
        "key", "state", "remote_recording_id", "media_device", "media_inode",
        "media_size", "source_mtime_ns", "file_last_modified_ms", "attempt_count",
        "last_error_code", "last_http_status", "transfer_started_at_ms", "accepted_at_ms",
        "published_at_ms", "created_at_ms", "updated_at_ms",
    }
    assert {field.name for field in fields(PublicationResult)} == {"job", "already_published"}
    assert _job().state is PublicationState.READY
    assert _job(PublicationState.TRANSFERRING).transfer_started_at_ms == 1_000
    assert _job(PublicationState.PUBLISHED).remote_recording_id == 9
    assert PublicationResult(_job(PublicationState.PUBLISHED), True).already_published
    _raises(PublicationResult, _job(), 1)
    _raises(_job, PublicationState.METADATA_PENDING, remote_recording_id=True)
    _raises(_job, PublicationState.METADATA_PENDING, attempt_count=0)
    _raises(_job, PublicationState.PUBLISHED, remote_recording_id=0)
    _raises(_job, PublicationState.PUBLISHED, attempt_count=0)
    _raises(_job, PublicationState.READY, transfer_started_at_ms=1_000)
    _raises(_job, PublicationState.PUBLISHED, published_at_ms=None)
    _raises(_job, PublicationState.TRANSFER_REJECTED, last_error_code="Bearer secret")
    _raises(_job, PublicationState.TRANSFER_REJECTED, last_http_status=True)
    pending = _job(PublicationState.METADATA_PENDING, last_error_code="metadata_failed", last_http_status=503)
    assert pending.last_error_code == "metadata_failed"
    result = PublicationResult(pending)
    assert result.error_code == "metadata_failed"
    assert result.http_status == 503
    assert "secret" not in repr(pending).casefold()
    try:
        pending.state = PublicationState.READY
        assert False, "publication jobs must be frozen"
    except AttributeError:
        pass


def test_unmatched_and_hidden_sidecars_use_current_media_fallbacks() -> None:
    media = Path("renamed.capture.mkv")
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


def test_visible_match_supports_public_cleaning_and_utc_dates() -> None:
    for description, location, expected in (
        ("Details", None, "Details"), (None, "Room", "Location: Room"),
        ("Details", "Room", "Details\n\nLocation: Room"),
    ):
        result = map_speakr_metadata(
            Path("current.mkv"), 0,
            _sidecar(_meeting(description=description, location=location)),
        )
        assert result.notes == expected
    cleaned = map_speakr_metadata(
        Path("  current.mkv"), 0,
        _sidecar(_meeting(description="a\x00 b", participants=("\tAlice\n",))),
    )
    assert cleaned.notes == "a b"
    assert cleaned.participants == "Alice"
    _raises(SpeakrMetadata, "Title", datetime.now(), "", "")
    _raises(SpeakrMetadata, "\x00", NOW, "", "")
    _raises(SpeakrMetadata, "Title", NOW, "", ["Alice"])
