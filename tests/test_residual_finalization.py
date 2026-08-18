"""Behavioral regression coverage for detached recording finalization."""

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from meeting_recorder.config import load_config
from meeting_recorder.controller import Controller, State
from meeting_recorder.domain import CaptureMode
from meeting_recorder.recorder import Recorder


class _Notifier:
    def __init__(self) -> None:
        self.calls = []

    def close_active(self) -> None:
        pass

    def info(self, *args, **kwargs) -> None:
        self.calls.append(args)


class _Widget:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _FailingRecorder:
    is_recording = False

    def __init__(self) -> None:
        self.sessions = []

    def start(self, _path, _app, _mode):
        raise RuntimeError("spawn failed")

    def attach_session(self, session) -> None:
        self.sessions.append(session)


def test_controller_start_exception_closes_visible_widget_and_releases_path() -> None:
    cfg = load_config()
    recorder = _FailingRecorder()
    controller = Controller(cfg, _Notifier(), recorder)
    widget = _Widget()
    controller._show_widget = lambda: setattr(controller, "_widget", widget)
    controller.start_manual()
    assert controller.state is State.IDLE
    assert widget.closed == 1 and not controller._reserved_paths
    assert recorder.sessions == [None]


def test_controller_ignore_and_pending_portal_stop_do_not_call_completion() -> None:
    cfg = load_config()
    notifier = _Notifier()
    recorder = _FailingRecorder()
    controller = Controller(cfg, notifier, recorder)
    seen = []
    controller.on_finished = seen.append
    controller.state = State.PROMPTING
    controller._on_user_ignore()
    assert controller.state is State.IDLE and seen == []

    controller.state = State.RECORDING
    controller._pending_path = Path("reserved.mkv")
    controller._reserved_paths.add(controller._pending_path)
    assert not controller.stop_manual()
    assert controller.state is State.IDLE and seen == [] and not controller._reserved_paths


def test_controller_reserves_existing_and_same_second_paths() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        base = root / "Meeting-2026-01-01.mkv"
        base.write_bytes(b"existing")
        controller = Controller(load_config(), _Notifier(), _FailingRecorder())
        first = controller._reserve_path(base)
        second = controller._reserve_path(base)
        assert first.name.endswith("-2.mkv") and second.name.endswith("-3.mkv")


def test_recorder_first_start_exception_resets_capture_state() -> None:
    import meeting_recorder.recorder as rec

    previous = rec.resolve_devices
    rec.resolve_devices = lambda *_args: (_ for _ in ()).throw(RuntimeError("devices"))
    try:
        recorder = Recorder(load_config())
        assert not recorder.start(Path("unreachable.mkv"), "Manual", CaptureMode.AUDIO_ONLY)
    finally:
        rec.resolve_devices = previous
    assert not recorder.is_recording
    assert recorder._proc is None and recorder._pump is None and recorder._fifo is None


def test_recorder_failed_finalize_cleans_parts_and_returns_one_immediate_failure() -> None:
    import meeting_recorder.recorder as rec

    class Proc:
        stdin = None

        def poll(self):
            return 0

    with TemporaryDirectory() as directory:
        root = Path(directory)
        target, part = root / "result.mkv", root / ".result.part0.mkv"
        part.write_bytes(b"segment")
        previous = rec.subprocess.Popen, rec.resolve_devices, rec.build_ffmpeg_cmd
        rec.subprocess.Popen = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("ffmpeg"))
        rec.resolve_devices = lambda *_args: None
        rec.build_ffmpeg_cmd = lambda *_args: ["ffmpeg"]
        try:
            recorder = Recorder(load_config(), clock=lambda: datetime.now(timezone.utc))
            recorder._final_path = target
            recorder._parts = [part]
            recorder._requested_capture_mode = CaptureMode.AUDIO_ONLY
            recorder._capture_started_at = datetime.now(timezone.utc)
            recorder._proc = Proc()
            handle = recorder.stop()
        finally:
            rec.subprocess.Popen, rec.resolve_devices, rec.build_ffmpeg_cmd = previous
        assert handle is not None and handle.poll() == (True, None)
        assert not part.exists() and not (root / ".result.concat.txt").exists()


def test_controller_dispatches_once_despite_stale_poll_and_notifier_failure() -> None:
    class Handle:
        target_path = Path("reserved.mkv")

        def poll(self):
            return True, None

    class BrokenNotifier(_Notifier):
        def info(self, *_args, **_kwargs) -> None:
            raise RuntimeError("notification failed")

    controller = Controller(load_config(), BrokenNotifier(), _FailingRecorder())
    handle = Handle()
    controller._handles.add(handle)
    controller._reserved_paths.add(handle.target_path)
    seen = []
    controller.on_finished = seen.append
    assert not controller._poll_handle(handle)
    assert seen == [None] and handle not in controller._handles
    assert not controller._poll_handle(handle) and seen == [None]


def test_controller_shutdown_drains_all_handles_after_one_wait_failure() -> None:
    class Handle:
        def __init__(self, fail: bool) -> None:
            self.target_path = None
            self.fail = fail
            self.waited = 0

        def wait(self):
            self.waited += 1
            if self.fail:
                raise RuntimeError("timeout")
            return None

        def abort(self):
            return None

    controller = Controller(load_config(), _Notifier(), _FailingRecorder())
    first, second = Handle(True), Handle(False)
    controller._handles.update((first, second))
    controller.shutdown()
    assert first.waited == second.waited == 1 and not controller._handles


def test_poll_exception_aborts_finalizer_cleans_and_dispatches_once() -> None:
    from meeting_recorder.recorder import FinalizationHandle, FinalizationSnapshot

    class Proc:
        returncode = None

        def __init__(self) -> None:
            self.killed = 0

        def poll(self):
            raise RuntimeError("poll failed")

        def kill(self) -> None:
            self.killed += 1
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    with TemporaryDirectory() as directory:
        root = Path(directory)
        part, listfile = root / ".part", root / ".list"
        part.write_bytes(b"part")
        listfile.write_text("part")
        now = datetime.now(timezone.utc)
        proc = Proc()
        handle = FinalizationHandle(
            proc, FinalizationSnapshot(root / "result.mkv", "Manual",
                                       CaptureMode.AUDIO_ONLY, True, False, now, now),
            [part], listfile)
        controller = Controller(load_config(), _Notifier(), _FailingRecorder())
        controller._handles.add(handle)
        seen = []
        controller.on_finished = seen.append
        assert not controller._poll_handle(handle)
        assert proc.killed == 1 and not part.exists() and not listfile.exists()
        assert seen == [None] and not controller._handles


def test_shutdown_wait_exception_aborts_reaps_and_cleans_handle() -> None:
    from meeting_recorder.recorder import FinalizationHandle, FinalizationSnapshot

    class Proc:
        returncode = None

        def __init__(self) -> None:
            self.killed = 0

        def poll(self):
            return None

        def wait(self, timeout=None):
            if self.killed:
                return -9
            raise RuntimeError("wait failed")

        def kill(self) -> None:
            self.killed += 1
            self.returncode = -9

    with TemporaryDirectory() as directory:
        root = Path(directory)
        part, listfile = root / ".part", root / ".list"
        part.write_bytes(b"part")
        listfile.write_text("part")
        now = datetime.now(timezone.utc)
        proc = Proc()
        handle = FinalizationHandle(
            proc, FinalizationSnapshot(root / "result.mkv", "Manual",
                                       CaptureMode.AUDIO_ONLY, True, False, now, now),
            [part], listfile)
        controller = Controller(load_config(), _Notifier(), _FailingRecorder())
        controller._handles.add(handle)
        controller.shutdown()
        assert proc.killed == 1 and not part.exists() and not listfile.exists()
        assert not controller._handles
