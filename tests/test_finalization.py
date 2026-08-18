"""Per-run finalization value and ordering tests."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from meeting_recorder.domain import CaptureMode, CompletedRecording
from meeting_recorder.recorder import FinalizationHandle, _FinalizationSnapshot


class _Proc:
    def __init__(self, timeout: bool = False) -> None:
        self.returncode: int | None = None
        self.timeout = timeout
        self.kills = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        if self.timeout and self.returncode is None:
            import subprocess
            raise subprocess.TimeoutExpired("finalize", timeout)
        return self.returncode

    def kill(self) -> None:
        self.kills += 1
        self.returncode = -9


def _snapshot(path: Path, app: str = "A", mode: CaptureMode = CaptureMode.AUDIO_VIDEO,
              video: bool = True) -> _FinalizationSnapshot:
    now = datetime.now(timezone.utc)
    return _FinalizationSnapshot(path, app, mode, True, video, now, now)


def test_completed_recording_is_frozen_and_validates_timestamps() -> None:
    now = datetime.now(timezone.utc)
    completed = CompletedRecording(Path("result.mkv"), "Zoom", CaptureMode.AUDIO_VIDEO,
                                   True, True, now, now)
    try:
        completed.path = Path("other.mkv")
        assert False, "CompletedRecording must be immutable"
    except AttributeError:
        pass

    for start, end in ((now.replace(tzinfo=None), now),
                       (now.replace(tzinfo=timezone(timedelta(hours=1))), now),
                       (now, now - timedelta(seconds=1))):
        try:
            CompletedRecording(Path("result.mkv"), "Zoom", CaptureMode.AUDIO_VIDEO,
                               True, True, start, end)
            assert False, "invalid capture timestamps must be rejected"
        except ValueError:
            pass


def test_handles_complete_out_of_order_with_their_own_metadata() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        first_path, second_path = root / "first.mkv", root / "second.mkv"
        first_path.write_bytes(b"a")
        second_path.write_bytes(b"b")
        first, second = _Proc(), _Proc()
        a = FinalizationHandle(first, _snapshot(first_path, "A", CaptureMode.AUDIO_ONLY,
                                                 False), [], None)
        b = FinalizationHandle(second, _snapshot(second_path, "B"), [], None)
        second.returncode = 0
        assert b.poll()[1].source_app == "B"
        assert a.poll() == (False, None)
        first.returncode = 0
        assert a.poll()[1].source_app == "A"


def test_handle_poll_wait_are_idempotent_and_cleanup_once() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        target, part, listfile = root / "saved.mkv", root / ".part", root / ".list"
        target.write_bytes(b"saved")
        part.write_bytes(b"part")
        listfile.write_text("part")
        proc = _Proc()
        handle = FinalizationHandle(proc, _snapshot(target), [part], listfile)
        proc.returncode = 0
        first = handle.poll()
        assert first[0] and first[1] is not None
        assert handle.poll() == first and handle.wait() is first[1]
        assert not part.exists() and not listfile.exists()


def test_handle_timeout_kills_reaps_and_cleans_once() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        target, part, listfile = root / "saved.mkv", root / ".part", root / ".list"
        part.write_bytes(b"part")
        listfile.write_text("part")
        proc = _Proc(timeout=True)
        handle = FinalizationHandle(proc, _snapshot(target), [part], listfile)
        assert handle.wait(timeout=0) is None
        assert proc.kills == 1 and not part.exists() and not listfile.exists()
        assert handle.wait(timeout=0) is None and proc.kills == 1
