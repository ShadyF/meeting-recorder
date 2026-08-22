"""Focused tests for the public Speakr publication domain."""

from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from meeting_recorder.calendar_domain import (
    CalendarOccurrence, CalendarParticipant, OccurrenceKey, meeting_snapshot,
)
from meeting_recorder.meeting_sidecar import MeetingSidecar
from meeting_recorder.speakr_domain import (
    CleanupClaim, CleanupIntent, CleanupPhase, MediaIdentity, PublicationJob, PublicationKey, PublicationOperation, PublicationResult,
    PublicationState, ResumeIntent, SpeakrMetadata, map_speakr_metadata, normalize_speakr_url,
)


NOW = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
HASH = "a" * 64


def _meeting(*, visible=True, title="  Design review  ", description=None, location=None, participants=()):
    return meeting_snapshot(CalendarOccurrence(
        OccurrenceKey.single("calendar", "event"), NOW, NOW + timedelta(hours=1),
        summary=title if visible else None, description=description, location=location,
        participants=tuple(CalendarParticipant(display_name=label) for label in participants),
        details_visible=visible,
    ))


def _sidecar(meeting=None) -> MeetingSidecar:
    return MeetingSidecar("renamed.mkv", "original.mkv", NOW, NOW + timedelta(minutes=1), meeting)


def _raises(callable_object, *args, **kwargs) -> None:
    try:
        callable_object(*args, **kwargs)
    except (TypeError, ValueError):
        return
    raise AssertionError("expected validation failure")


def _job(state=PublicationState.QUEUED, **changes) -> PublicationJob:
    values: dict[str, Any] = dict(
        job_id="job-1", key=PublicationKey("HTTPS://EXAMPLE.COM:443/", HASH.upper()),
        private_path=b"/private/staged-bytes", state=state, created_at_ms=1_000, updated_at_ms=1_000,
    )
    if state is PublicationState.TRANSFERRING:
        values.update(attempt_count=1, operation="none", resume_intent="post",
                      reconciliation_token="token-1", transfer_started_at_ms=1_000)
    elif state is PublicationState.METADATA_PENDING:
        values.update(attempt_count=1, remote_recording_id=9, operation="patch", resume_intent="patch",
                      transfer_started_at_ms=1_000, accepted_at_ms=2_000, updated_at_ms=2_000)
    elif state is PublicationState.PUBLISHED:
        values.update(attempt_count=1, remote_recording_id=9, operation="none", resume_intent="none",
                      transfer_started_at_ms=1_000, accepted_at_ms=2_000,
                      published_at_ms=3_000, updated_at_ms=3_000)
    elif state is PublicationState.UNCERTAIN:
        values.update(attempt_count=1, operation="reconcile", resume_intent="reconcile",
                      reconciliation_token="token-1", uncertain_at_ms=2_000, updated_at_ms=2_000)
    elif state in {PublicationState.BLOCKED, PublicationState.MISSING, PublicationState.LOCAL_REMOVED}:
        values.update(operation="none", resume_intent="none")

        # Supply the complete publication audit chain for local removal fixtures.
        if state is PublicationState.LOCAL_REMOVED:
            values.update(
                private_path=None, remote_recording_id=9, next_attempt_at_ms=0,
                transfer_started_at_ms=1_000, accepted_at_ms=2_000,
                published_at_ms=3_000, local_removed_at_ms=4_000, updated_at_ms=4_000,
            )
    values.update(changes)
    return PublicationJob(**values)


def test_state_vocabulary_is_exact_and_job_id_is_public_stable_identity() -> None:
    assert [state.value for state in PublicationState] == [
        "queued", "transferring", "metadata_pending", "published",
        "uncertain", "blocked", "missing", "local_removed",
    ]
    names = {field.name for field in fields(PublicationJob)}
    assert {"job_id", "key", "operation", "resume_intent", "private_path",
            "reconciliation_token", "remote_recording_id", "lease_generation",
            "lease_owner", "lease_expires_at_ms", "next_attempt_at_ms"} <= names
    assert "max_attempts" not in names
    assert _job().job_id == "job-1"
    assert not hasattr(_job(), "media_path")
    assert _job().http_method == "POST"
    assert _job(PublicationState.TRANSFERRING).operation == "none"
    assert _job(PublicationState.TRANSFERRING).http_method is None
    assert _job(PublicationState.METADATA_PENDING).http_method == "PATCH"
    assert _job(PublicationState.UNCERTAIN).http_method is None
    assert PublicationOperation.POST.value == "post"
    assert ResumeIntent.RECONCILE.value == "reconcile"


def test_domain_rejects_unsafe_identity_and_inconsistent_operation() -> None:
    assert PublicationKey("HTTP://Example.com:80/", HASH.upper()).instance_url == "http://example.com"
    assert _job().private_path == b"/private/staged-bytes"
    _raises(PublicationKey, "https://example.com", "g" * 64)
    _raises(PublicationKey, "https://example.com?token=secret", HASH)
    _raises(_job, operation="patch", resume_intent="patch")
    _raises(_job, PublicationState.METADATA_PENDING, remote_recording_id=None)
    _raises(_job, PublicationState.TRANSFERRING, reconciliation_token=None)
    assert _job(attempt_count=100_000).attempt_count == 100_000
    _raises(_job, last_error_code="Bearer secret")
    _raises(_job, last_http_status=True)


def test_media_identity_and_linux_filename_bytes_are_validated() -> None:
    identity = MediaIdentity(Path("recording.mkv"), 1, 2, 3, 4_000_000_000)
    assert identity.path == Path("recording.mkv")
    assert _job(private_path=b"/private/\xff-recording.mkv").private_path == b"/private/\xff-recording.mkv"
    _raises(_job, private_path=b"bad\x00path")
    _raises(replace, identity, inode=-1)
    _raises(_job, PublicationState.PUBLISHED, published_at_ms=None)
    result = PublicationResult(_job(PublicationState.PUBLISHED), True)
    assert result.already_published and result.error_code is None


def test_blocked_missing_preserve_resume_intent_and_uncertain_has_active_terminal_forms() -> None:
    for intent in ("post", "reconcile", "patch", "none"):
        remote = 9 if intent == "patch" else None
        blocked = _job(PublicationState.BLOCKED, resume_intent=intent, remote_recording_id=remote)
        missing = _job(PublicationState.MISSING, resume_intent=intent, remote_recording_id=remote)
        assert blocked.operation == "none" and blocked.resume_intent == intent
        assert missing.operation == "none" and missing.resume_intent == intent

    active = _job(PublicationState.UNCERTAIN)
    terminal = _job(PublicationState.UNCERTAIN, operation="none")
    assert active.reconciliation_eligible
    assert not terminal.reconciliation_eligible
    _raises(_job, PublicationState.UNCERTAIN, operation="post")


def test_metadata_projection_excludes_hidden_and_unmatched_meeting_details() -> None:
    media = Path("renamed.capture.mkv")
    mtime_ns = 1_735_689_123_456_789_000
    assert map_speakr_metadata(media, mtime_ns, None).title == "renamed.capture"
    assert map_speakr_metadata(media, mtime_ns, _sidecar()).meeting_date == NOW
    hidden = map_speakr_metadata(media, mtime_ns, _sidecar(_meeting(visible=False)))
    assert hidden.title == "renamed.capture" and hidden.notes == "" and hidden.participants == ""


def test_visible_metadata_is_sanitized_and_utc() -> None:
    meeting = _meeting(
        description="  First line  \r\nSecond\tline  ", location="  Room 7 ",
        participants=(" Alice ", "Bob\tSmith"),
    )
    result = map_speakr_metadata(Path("current-name.mkv"), 4_000_000_000, _sidecar(meeting))
    assert result == SpeakrMetadata("Design review", NOW, "First line\nSecond line\n\nLocation: Room 7", "Alice, Bob Smith")
    _raises(SpeakrMetadata, "Title", datetime.now(), "", "")


def test_cleanup_intent_is_bounded_and_requires_complete_sidecar_identity() -> None:
    # Build one complete intent to establish the accepted cleanup shape.
    intent = CleanupIntent(
        "cleanup-1", b"/private/recording", HASH, 1, 2, 3, 4,
        quarantine_media_basename="media.tmp", phase=CleanupPhase.PREPARED,
        created_at_ms=1_000, updated_at_ms=1_000,
    )

    # Confirm the public fields retain their bounded path and digest values.
    assert intent.expected_private_path == b"/private/recording"
    assert intent.expected_recording_sha256 == HASH

    # Reject unsafe IDs, relative paths, incomplete sidecar identity, and nlink data.
    _raises(CleanupIntent, "cleanup/unsafe", b"/private/recording", HASH, 1, 2, 3, 4)
    _raises(CleanupIntent, "cleanup-2", b"relative", HASH, 1, 2, 3, 4)
    _raises(CleanupIntent, "cleanup-3", b"/private/recording", HASH, 1, 2, 3, 4, sidecar_device=1)
    _raises(CleanupIntent, "cleanup-4", b"/private/recording", HASH, 1, 2, 3, 4, claimed_job_ids=("job",), claimed_lease_generations=())
    _raises(CleanupIntent, "cleanup-5", b"/private/recording", HASH, 1, 2, 3, 4, media_nlink=2)


def test_local_removed_requires_complete_positive_remote_audit_chain() -> None:
    # Reject removal rows missing any required audit timestamp or remote ID.
    _raises(_job, PublicationState.LOCAL_REMOVED, remote_recording_id=None)
    _raises(_job, PublicationState.LOCAL_REMOVED, transfer_started_at_ms=None)
    _raises(_job, PublicationState.LOCAL_REMOVED, accepted_at_ms=900)
    _raises(_job, PublicationState.LOCAL_REMOVED, published_at_ms=1_500)
    # Preserve the valid complete audit chain as the control case.
    assert _job(PublicationState.LOCAL_REMOVED).remote_recording_id == 9


def test_cleanup_claim_is_immutable_and_exactly_fenced() -> None:
    # Build a claim whose job IDs and lease generations align exactly.
    claim = CleanupClaim("cleanup-1", "owner-1", ("job-1", "job-2"), (1, 2))
    assert claim.job_ids == ("job-1", "job-2")

    # Reject reordered, missing, or zero-generation claim members.
    _raises(CleanupClaim, "cleanup-1", "owner-1", ("job-2", "job-1"), (1, 2))
    _raises(CleanupClaim, "cleanup-1", "owner-1", ("job-1",), (0,))
