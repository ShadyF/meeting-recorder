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


def test_overlapping_recorder_runs_dispatch_their_own_completed_recordings() -> None:
    """A detached finalizer must never borrow metadata from the next run."""
    import meeting_recorder.recorder as rec

    class Proc:
        def __init__(self, command, *_args, **_kwargs) -> None:
            self.command = command
            self.returncode = None
            self.stdin = None
            if command[0] == "capture":
                Path(command[-1]).write_bytes(b"part")

        def poll(self):
            return self.returncode

        def wait(self, _timeout=None, **_kwargs):
            return self.returncode

    with TemporaryDirectory() as directory:
        root = Path(directory)
        times = iter(datetime(2026, 1, 1, hour, tzinfo=timezone.utc)
                     for hour in (1, 2, 3, 4))
        finals = {}
        previous = (rec.subprocess.Popen, rec.resolve_devices, rec.build_ffmpeg_cmd,
                    rec.build_finalize_cmd, rec.Recorder._wants_pipewire)
        rec.subprocess.Popen = lambda command, *args, **kwargs: (
            finals.setdefault(Path(command[-1]), Proc(command, *args, **kwargs))
            if command[0] == "finalize" else Proc(command, *args, **kwargs))
        rec.resolve_devices = lambda *_args: None
        rec.build_ffmpeg_cmd = lambda _cfg, output, _dev, _mode: ["capture", str(output)]
        rec.build_finalize_cmd = lambda _cfg, _list, dest, *_args: ["finalize", str(dest)]
        rec.Recorder._wants_pipewire = property(lambda _self: False)
        try:
            recorder = Recorder(load_config(), clock=lambda: next(times))
            first_path, second_path = root / "first.mkv", root / "second.mkv"
            assert recorder.start(first_path, "A", CaptureMode.AUDIO_ONLY)
            first = recorder.stop()
            assert recorder.start(second_path, "B", CaptureMode.AUDIO_VIDEO)
            second = recorder.stop()
        finally:
            (rec.subprocess.Popen, rec.resolve_devices, rec.build_ffmpeg_cmd,
             rec.build_finalize_cmd, rec.Recorder._wants_pipewire) = previous

        controller = Controller(load_config(), _Notifier(), recorder)
        completed = []
        controller.on_finished = completed.append
        controller._handles.update((first, second))
        controller._reserved_paths.update((first_path, second_path))

        second_path.write_bytes(b"second")
        finals[second_path].returncode = 0
        assert not controller._poll_handle(second)
        first_path.write_bytes(b"first")
        finals[first_path].returncode = 0
        assert not controller._poll_handle(first)
        assert not controller._poll_handle(first)

        assert [(item.path, item.source_app, item.requested_capture_mode,
                 item.has_video, item.capture_started_at, item.capture_ended_at)
                for item in completed] == [
            (second_path, "B", CaptureMode.AUDIO_VIDEO, True,
             datetime(2026, 1, 1, 3, tzinfo=timezone.utc),
             datetime(2026, 1, 1, 4, tzinfo=timezone.utc)),
            (first_path, "A", CaptureMode.AUDIO_ONLY, False,
             datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
             datetime(2026, 1, 1, 2, tzinfo=timezone.utc)),
        ]


def test_portal_denial_finalizes_requested_video_as_audio_only() -> None:
    import meeting_recorder.recorder as rec

    class Proc:
        def __init__(self, command, *_args, **_kwargs) -> None:
            self.command = command
            self.returncode = None
            self.stdin = None
            if command[0] == "capture":
                Path(command[-1]).write_bytes(b"part")

        def poll(self):
            return self.returncode

        def wait(self, _timeout=None, **_kwargs):
            return self.returncode

    with TemporaryDirectory() as directory:
        root = Path(directory)
        finals = {}
        previous = (rec.subprocess.Popen, rec.resolve_devices, rec.build_ffmpeg_cmd,
                    rec.build_finalize_cmd, rec.Recorder._wants_pipewire)
        rec.subprocess.Popen = lambda command, *args, **kwargs: (
            finals.setdefault(Path(command[-1]), Proc(command, *args, **kwargs))
            if command[0] == "finalize" else Proc(command, *args, **kwargs))
        rec.resolve_devices = lambda *_args: rec.CaptureDevices(":0", "1x1", 0, 0, "m", "s")
        rec.build_ffmpeg_cmd = lambda _cfg, output, _dev, _mode: ["capture", str(output)]
        rec.build_finalize_cmd = lambda _cfg, _list, dest, *_args: ["finalize", str(dest)]
        rec.Recorder._wants_pipewire = property(lambda _self: True)
        try:
            target = root / "denied.mkv"
            recorder = Recorder(load_config())
            assert recorder.start(target, "Portal", CaptureMode.AUDIO_VIDEO)
            handle = recorder.stop()
        finally:
            (rec.subprocess.Popen, rec.resolve_devices, rec.build_ffmpeg_cmd,
             rec.build_finalize_cmd, rec.Recorder._wants_pipewire) = previous

        target.write_bytes(b"audio")
        finals[target].returncode = 0
        completed = handle.poll()[1]
        assert completed is not None
        assert completed.requested_capture_mode is CaptureMode.AUDIO_VIDEO
        assert completed.has_video is False


def test_widget_stop_cancels_pending_manual_portal_without_completion() -> None:
    class Session:
        def __init__(self) -> None:
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    with TemporaryDirectory() as directory:
        path = Path(directory) / "pending.mkv"
        recorder = _FailingRecorder()
        controller = Controller(load_config(), _Notifier(), recorder)
        widget, session = _Widget(), Session()
        finished, cancelled = [], []
        controller._widget = widget
        controller._session = session
        controller._pending_path = path
        controller._reserved_paths.add(path)
        controller._manual = True
        controller.state = State.RECORDING
        controller.on_finished = finished.append
        controller.on_manual_cancelled = lambda: cancelled.append(True)

        controller._on_widget_stop()

        assert cancelled == [True] and finished == []
        assert controller.state is State.IDLE and controller._widget is None
        assert session.closed == 1 and not controller._reserved_paths
        assert recorder.sessions == [None]
