"""Per-run finalization value and ordering tests."""

from datetime import datetime, timezone
from pathlib import Path

from meeting_recorder.domain import CaptureMode, CompletedRecording
from meeting_recorder.recorder import FinalizationHandle


class _Proc:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_completed_recording_is_frozen_and_uses_utc_timestamps():
    now = datetime.now(timezone.utc)
    completed = CompletedRecording(Path("/tmp/a.mkv"), "Zoom", CaptureMode.AUDIO_VIDEO,
                                   True, True, now, now)
    assert completed.capture_started_at.tzinfo is timezone.utc
    try:
        completed.path = Path("/tmp/b.mkv")
        assert False, "CompletedRecording must be immutable"
    except AttributeError:
        pass


def test_handles_complete_out_of_order_with_their_own_metadata(tmp_path=Path("/tmp")):
    started = datetime.now(timezone.utc)
    first_path, second_path = tmp_path / "first.mkv", tmp_path / "second.mkv"
    first_path.write_bytes(b"a")
    second_path.write_bytes(b"b")
    first, second = _Proc(), _Proc()
    a = FinalizationHandle(first, first_path, [], None, "A", CaptureMode.AUDIO_ONLY,
                           True, False, started, started)
    b = FinalizationHandle(second, second_path, [], None, "B", CaptureMode.AUDIO_VIDEO,
                           True, True, started, started)
    second.returncode = 0
    assert b.poll()[1].source_app == "B"
    assert a.poll() == (False, None)
    first.returncode = 0
    assert a.poll()[1].source_app == "A"
    first_path.unlink(missing_ok=True)
    second_path.unlink(missing_ok=True)
