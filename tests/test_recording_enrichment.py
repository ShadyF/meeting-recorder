"""Cache-only enrichment and correction transaction behavior."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import meeting_recorder.recording_enrichment as enrichment_module
from meeting_recorder.calendar_domain import (
    CalendarOccurrence, OccurrenceKey, meeting_snapshot,
)
from meeting_recorder.domain import CaptureMode, CompletedRecording
from meeting_recorder.meeting_sidecar import MeetingSidecar, load_sidecar, sidecar_path, write_sidecar
from meeting_recorder.recording_enrichment import (
    CorrectionTransactionError, RecordingCorrectionService, RecordingEnricher,
)


NOW = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)


def _occurrence(event: str = "event", summary: str | None = "Design review",
                visible: bool = True, start: datetime = NOW) -> CalendarOccurrence:
    return CalendarOccurrence(
        OccurrenceKey.single("calendar", event), start, start + timedelta(hours=1),
        summary=summary, details_visible=visible)


def _completed(path: Path, start: datetime = NOW,
               end: datetime = NOW + timedelta(minutes=30)) -> CompletedRecording:
    return CompletedRecording(path, "Manual", CaptureMode.AUDIO_VIDEO, True, True, start, end)


def _write_capture(path: Path, meeting=None, fallback: str | None = None) -> None:
    path.write_bytes(b"recording")
    write_sidecar(
        sidecar_path(path),
        MeetingSidecar(path.name, fallback or path.name, NOW, NOW + timedelta(minutes=30), meeting),
    )


def test_enrich_visible_match_moves_media_and_writes_snapshot_sidecar() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "capture.mkv"
        source.write_bytes(b"recording")
        original = _completed(source)
        result = RecordingEnricher([_occurrence()], lambda value: value).enrich(original)
        assert result is not original and result.path != source
        assert result.path.exists() and not source.exists()
        metadata = load_sidecar(sidecar_path(result.path))
        assert metadata.recording_filename == result.path.name
        assert metadata.meeting is not None and metadata.meeting.title == "Design review"
        assert original.path == source


def test_enrich_hidden_and_unmatched_keep_fallback_name_but_write_sidecar() -> None:
    for occurrences, expected_meeting in (([_occurrence(visible=False)], True), ([], False)):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "capture.mkv"
            source.write_bytes(b"recording")
            result = RecordingEnricher(occurrences, lambda value: value).enrich(_completed(source))
            metadata = load_sidecar(sidecar_path(source))
            assert result.path == source and (metadata.meeting is not None) == expected_meeting
            if expected_meeting:
                assert metadata.meeting is not None and metadata.meeting.title is None


def test_enrich_ambiguous_match_and_provider_exception_preserve_media() -> None:
    with TemporaryDirectory() as directory:
        source = Path(directory) / "capture.mkv"
        source.write_bytes(b"recording")
        original = _completed(source)
        tied = [_occurrence("left"), _occurrence("right")]
        assert RecordingEnricher(tied).enrich(original).path == source

        class BrokenProvider:
            def __call__(self):
                raise RuntimeError("cache unavailable")

        assert RecordingEnricher(BrokenProvider()).enrich(original).path == source
        assert load_sidecar(sidecar_path(source)).meeting is None


def test_enrich_failures_preserve_authoritative_media_or_return_safe_replacement() -> None:
    with TemporaryDirectory() as directory:
        source = Path(directory) / "capture.mkv"
        source.write_bytes(b"recording")
        original_writer = enrichment_module.write_sidecar
        enrichment_module.write_sidecar = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("metadata"))
        try:
            assert RecordingEnricher([_occurrence()]).enrich(_completed(source)).path == source
        finally:
            enrichment_module.write_sidecar = original_writer
        assert source.exists()

        original_move = enrichment_module.move_regular_file_no_replace
        enrichment_module.move_regular_file_no_replace = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("move"))
        try:
            assert RecordingEnricher([_occurrence()]).enrich(_completed(source)).path == source
        finally:
            enrichment_module.move_regular_file_no_replace = original_move

        original_relocate = enrichment_module._write_moved_sidecar
        enrichment_module._write_moved_sidecar = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("relocate"))
        try:
            result = RecordingEnricher([_occurrence()]).enrich(_completed(source))
        finally:
            enrichment_module._write_moved_sidecar = original_relocate
        assert result.path.exists()
        recovered = RecordingCorrectionService().discover(result.path)
        assert recovered is not None and recovered.recording_filename == result.path.name


def test_enrich_does_not_mutate_input_or_react_to_later_occurrence_mutation() -> None:
    with TemporaryDirectory() as directory:
        source = Path(directory) / "capture.mkv"
        source.write_bytes(b"recording")
        original = _completed(source)
        occurrence = _occurrence()
        result = RecordingEnricher([occurrence], lambda value: value).enrich(original)
        before = load_sidecar(sidecar_path(result.path))
        changed = replace(occurrence, summary="Changed", details_visible=True)
        assert before.meeting is not None
        assert changed.summary == "Changed" and before.meeting.title == "Design review"
        assert original.path == source


def test_correction_discovery_list_order_select_switch_clear_and_missing() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "capture.mkv"
        _write_capture(source)
        first, second = _occurrence("first", "First", start=NOW - timedelta(hours=2)), _occurrence(
            "second", "Second", start=NOW - timedelta(minutes=10))
        service = RecordingCorrectionService([first, second], lambda value: value)
        nearby = service.list_nearby(source)
        assert [item.key.event_id for item in nearby] == ["second", "first"]
        assert not any(item.is_current for item in nearby)
        selected = service.select(source, second.key, [first, second])
        assert selected.exists() and selected != source
        metadata = load_sidecar(sidecar_path(selected))
        assert metadata.meeting is not None and metadata.meeting.occurrence_key == second.key
        cleared = service.clear(selected)
        assert cleared.name == "capture.mkv" and cleared.exists()
        assert not sidecar_path(cleared).exists()
        assert RecordingCorrectionService().clear(cleared) == cleared


def test_correction_hidden_selection_uses_fallback_and_exact_cached_key() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "visible.mkv"
        _write_capture(source)
        hidden = _occurrence("hidden", None, visible=False)
        service = RecordingCorrectionService([hidden])
        selected = service.select(source, hidden.key)
        assert selected.name == "visible.mkv"
        hidden_metadata = load_sidecar(sidecar_path(selected))
        assert hidden_metadata.meeting is not None and hidden_metadata.meeting.title is None
        try:
            service.select(selected, OccurrenceKey.single("calendar", "missing"), [hidden])
            assert False, "unknown cached key must be rejected"
        except ValueError:
            pass


def test_correction_ambiguous_or_malformed_adjacent_sidecars_do_not_write() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "capture.mkv"
        source.write_bytes(b"recording")
        first = root / "first.meeting.json"
        second = root / "second.meeting.json"
        payload = MeetingSidecar(source.name, source.name, NOW, NOW + timedelta(minutes=1), None)
        write_sidecar(first, payload)
        write_sidecar(second, payload)
        service = RecordingCorrectionService([])
        try:
            service.discover(source)
            assert False, "ambiguous sidecars must fail"
        except ValueError:
            pass
        second.write_text("not json", encoding="utf-8")
        assert service.discover(source) is not None
        assert source.read_bytes() == b"recording"


def test_correction_discovery_recovers_pre_move_and_post_move_crash_sidecars() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        old = root / "capture.mkv"
        old.write_bytes(b"recording")
        intended = root / "2026-08-18_12-00-00_Review.mkv"
        write_sidecar(sidecar_path(old), MeetingSidecar(
            intended.name, old.name, NOW, NOW + timedelta(minutes=1), None))
        service = RecordingCorrectionService(())
        recovered_old = service.discover(old)
        assert recovered_old is not None and recovered_old.recording_filename == intended.name

        relocated = root / "relocated.mkv"
        relocated.write_bytes(b"recording")
        stale_sidecar = root / "old-name.meeting.json"
        write_sidecar(stale_sidecar, MeetingSidecar(
            relocated.name, "original.mkv", NOW, NOW + timedelta(minutes=1), None))
        recovered_relocated = service.discover(relocated)
        assert recovered_relocated is not None and recovered_relocated.recording_filename == relocated.name


def test_correction_nearby_ties_use_start_distance_then_start_and_key() -> None:
    with TemporaryDirectory() as directory:
        source = Path(directory) / "capture.mkv"
        _write_capture(source)
        far_start = _occurrence("z", "Far", start=NOW - timedelta(hours=1))
        near_start = _occurrence("b", "Near", start=NOW + timedelta(minutes=5))
        same_start_a = _occurrence("a", "A", start=NOW + timedelta(minutes=5))
        rows = RecordingCorrectionService((far_start, near_start, same_start_a)).list_nearby(source)
        assert [row.key.event_id for row in rows] == ["a", "b", "z"]


def test_correction_clear_collision_keeps_media_authoritative() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "renamed.mkv"
        _write_capture(source, fallback="original.mkv")
        (root / "original.mkv").write_bytes(b"other")
        result = RecordingCorrectionService().clear(source)
        assert result.name == "original-2.mkv" and result.read_bytes() == b"recording"
        assert (root / "original.mkv").read_bytes() == b"other"


def test_active_controller_reservation_forces_clear_collision_and_preserves_late_file() -> None:
    from meeting_recorder.config import load_config
    from meeting_recorder.controller import Controller
    from meeting_recorder.recording_paths import move_regular_file_no_replace

    class Notifier:
        pass

    with TemporaryDirectory() as directory, TemporaryDirectory() as cache:
        import os
        original_cache = os.environ.get("XDG_CACHE_HOME")
        controller = None
        reserved = None
        os.environ["XDG_CACHE_HOME"] = cache
        try:
            root = Path(directory)
            fallback = root / "fallback.mkv"
            controller = Controller(load_config(), Notifier(), object())  # type: ignore[arg-type]
            reserved = controller._reserve_path(fallback)
            source = root / "renamed.mkv"
            _write_capture(source, fallback=fallback.name)
            selected = RecordingCorrectionService().clear(source)
            assert selected.name == "fallback-2.mkv"
            assert selected.read_bytes() == b"recording"

            # The finalizer can still publish its reserved target while correction
            # has moved the cleared media to the reservation-safe collision name.
            fallback.write_bytes(b"finalized")
            assert fallback.read_bytes() == b"finalized"
            assert selected.read_bytes() == b"recording"

            # A foreign writer bypassing the reservation cannot replace the active target.
            late_source = root / "late.mkv"
            late_source.write_bytes(b"finalized")
            late_target = root / "late-target.mkv"
            late_target.write_bytes(b"foreign")
            try:
                move_regular_file_no_replace(late_source, late_target)
                assert False, "a late foreign target must never be overwritten"
            except FileExistsError:
                pass
            assert fallback.read_bytes() == b"finalized"
            assert late_target.read_bytes() == b"foreign"
            assert late_source.read_bytes() == b"finalized"
        finally:
            if controller is not None and reserved is not None:
                controller._release_path(reserved)
            if original_cache is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = original_cache


def test_correction_select_and_clear_restore_the_exact_sidecar_on_move_failure() -> None:
    original_move = enrichment_module.move_regular_file_no_replace
    try:
        def fail_move(*_args, **_kwargs):
            raise OSError("move failed")

        enrichment_module.move_regular_file_no_replace = fail_move
        for operation in ("visible", "hidden", "clear"):
            with TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "capture.mkv"
                original = MeetingSidecar(source.name, "fallback.mkv", NOW,
                                          NOW + timedelta(minutes=30),
                                          meeting_snapshot(_occurrence("original", "Original")))
                source.write_bytes(b"recording")
                write_sidecar(sidecar_path(source), original)
                service = RecordingCorrectionService()
                try:
                    if operation == "visible":
                        service.select(source, _occurrence("visible", "Visible").key,
                                       [_occurrence("visible", "Visible")])
                    elif operation == "hidden":
                        hidden = _occurrence("hidden", None, visible=False)
                        service.select(source, hidden.key, [hidden])
                    else:
                        service.clear(source)
                    assert False, "failed correction must not report success"
                except CorrectionTransactionError as error:
                    assert error.outcome.current_path == source
                assert source.exists()
                assert load_sidecar(sidecar_path(source)) == original
    finally:
        enrichment_module.move_regular_file_no_replace = original_move


def test_correction_postcommit_failures_expose_the_actual_media_path() -> None:
    original_relocate = enrichment_module._write_moved_sidecar
    try:
        enrichment_module._write_moved_sidecar = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(OSError("relocation failed")))
        with TemporaryDirectory() as directory:
            source = Path(directory) / "capture.mkv"
            _write_capture(source, fallback="fallback.mkv")
            visible = _occurrence("visible", "Visible")
            try:
                RecordingCorrectionService([visible], lambda value: value).select(
                    source, visible.key, [visible])
                assert False, "post-move sidecar failure must not report success"
            except CorrectionTransactionError as error:
                assert error.outcome.partial and error.outcome.committed
                assert error.outcome.current_path.exists()
    finally:
        enrichment_module._write_moved_sidecar = original_relocate


def test_correction_clear_sidecar_removal_failure_keeps_moved_media_authoritative() -> None:
    original_remove = enrichment_module.remove_sidecar
    try:
        enrichment_module.remove_sidecar = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(OSError("removal failed")))
        with TemporaryDirectory() as directory:
            source = Path(directory) / "renamed.mkv"
            _write_capture(source, fallback="fallback.mkv")
            try:
                RecordingCorrectionService().clear(source)
                assert False, "sidecar removal failure must not report success"
            except CorrectionTransactionError as error:
                assert error.outcome.partial and error.outcome.committed
                assert error.outcome.current_path.exists()
            assert not source.exists()
    finally:
        enrichment_module.remove_sidecar = original_remove


def test_correction_clear_pre_move_sidecar_failure_restores_exact_sidecar() -> None:
    original_remove = enrichment_module.remove_sidecar
    try:
        enrichment_module.remove_sidecar = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(OSError("removal failed")))
        with TemporaryDirectory() as directory:
            source = Path(directory) / "capture.mkv"
            original = MeetingSidecar(source.name, source.name, NOW,
                                      NOW + timedelta(minutes=30),
                                      meeting_snapshot(_occurrence("original", "Original")))
            source.write_bytes(b"recording")
            write_sidecar(sidecar_path(source), original)
            try:
                RecordingCorrectionService().clear(source)
                assert False, "pre-move sidecar failure must not report success"
            except CorrectionTransactionError as error:
                assert not error.outcome.committed and not error.outcome.partial
            assert source.exists() and load_sidecar(sidecar_path(source)) == original
    finally:
        enrichment_module.remove_sidecar = original_remove


def test_correction_recovers_same_inode_duplicates_from_both_names_and_refuses_foreign_inode() -> None:
    for passed_name in ("old.mkv", "new.mkv"):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = root / "old.mkv", root / "new.mkv"
            old.write_bytes(b"recording")
            new.hardlink_to(old)
            write_sidecar(sidecar_path(old), MeetingSidecar(
                new.name, old.name, NOW, NOW + timedelta(minutes=1), None))
            found = RecordingCorrectionService().discover(root / passed_name)
            assert found is not None and found.recording_filename == new.name
            assert new.exists() and not old.exists()
            assert load_sidecar(sidecar_path(new)).recording_filename == new.name

    with TemporaryDirectory() as directory:
        root = Path(directory)
        old, new = root / "old.mkv", root / "new.mkv"
        old.write_bytes(b"old")
        new.write_bytes(b"foreign")
        write_sidecar(sidecar_path(old), MeetingSidecar(
            new.name, old.name, NOW, NOW + timedelta(minutes=1), None))
        try:
            RecordingCorrectionService().discover(old)
            assert False, "different inodes must never be merged"
        except ValueError:
            pass
        assert old.exists() and new.read_bytes() == b"foreign"


def test_enricher_notifies_once_after_committed_media_and_sidecar_move() -> None:
    with TemporaryDirectory() as directory:
        source = Path(directory) / "capture.mkv"
        source.write_bytes(b"recording")
        moves: list[tuple[Path, Path]] = []
        result = RecordingEnricher(
            [_occurrence()], on_media_renamed=lambda old, new: moves.append((old, new)),
        ).enrich(_completed(source))
        assert result.path != source
        assert moves == [(source, result.path)]


def test_enricher_rename_callback_isolated_and_not_called_for_noop_or_failed_move() -> None:
    with TemporaryDirectory() as directory:
        source = Path(directory) / "capture.mkv"
        source.write_bytes(b"recording")
        moves: list[tuple[Path, Path]] = []
        no_op = RecordingEnricher(
            [], on_media_renamed=lambda old, new: moves.append((old, new)),
        ).enrich(_completed(source))
        assert no_op.path == source and moves == []

        original_move = enrichment_module.move_regular_file_no_replace
        enrichment_module.move_regular_file_no_replace = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(OSError("move failed")))
        try:
            failed = RecordingEnricher(
                [_occurrence()], on_media_renamed=lambda old, new: moves.append((old, new)),
            ).enrich(_completed(source))
        finally:
            enrichment_module.move_regular_file_no_replace = original_move
        assert failed.path == source and moves == []

        def broken_callback(_old: Path, _new: Path) -> None:
            raise RuntimeError("publication state unavailable")

        result = RecordingEnricher([_occurrence()], on_media_renamed=broken_callback).enrich(
            _completed(source),
        )
        assert result.path != source


def test_correction_notifies_select_and_clear_only_after_successful_commits() -> None:
    with TemporaryDirectory() as directory:
        source = Path(directory) / "capture.mkv"
        _write_capture(source)
        moves: list[tuple[Path, Path]] = []
        service = RecordingCorrectionService(
            [_occurrence()], on_media_renamed=lambda old, new: moves.append((old, new)),
        )
        selected = service.select(source, _occurrence().key)
        assert moves == [(source, selected)]
        cleared = service.clear(selected)
        assert moves == [(source, selected), (selected, cleared)]

        original_move = enrichment_module.move_regular_file_no_replace
        enrichment_module.move_regular_file_no_replace = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(OSError("move failed")))
        try:
            _write_capture(cleared)
            try:
                RecordingCorrectionService(
                    [_occurrence("failed")],
                    on_media_renamed=lambda old, new: moves.append((old, new)),
                ).select(cleared, _occurrence("failed").key)
            except CorrectionTransactionError:
                pass
        finally:
            enrichment_module.move_regular_file_no_replace = original_move
        assert moves == [(source, selected), (selected, cleared)]

        isolated = Path(directory) / "isolated.mkv"
        _write_capture(isolated)
        def broken_callback(_old: Path, _new: Path) -> None:
            raise RuntimeError("publication state unavailable")
        isolated_result = RecordingCorrectionService(
            [_occurrence("isolated")], on_media_renamed=broken_callback,
        ).select(isolated, _occurrence("isolated").key)
        assert isolated_result.exists()
