"""Unit tests for the two-stage capture/finalize ffmpeg command construction."""

import os
from pathlib import Path

from meeting_recorder.config import load_config
from meeting_recorder.domain import CaptureMode, VideoSource
from meeting_recorder.recorder import (
    CaptureDevices,
    audio_roles,
    build_ffmpeg_cmd,
    build_finalize_cmd,
    build_pipewire_cmd,
    clamp_region,
    parse_region,
    pipewire_capture_size,
    pipewire_region,
    video_geometry,
)

DEV = CaptureDevices(
    display=":0",
    video_size="1920x1080",
    video_x=0,
    video_y=0,
    mic_source="mic_src",
    monitor_source="sink.monitor",
)
OUT = Path("/tmp/out.mkv")
LIST = Path("/tmp/list.txt")


def _cfg(**over):
    cfg = load_config()
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


# -- stage 1: live capture ------------------------------------------------

def test_capture_records_raw_separate_audio_tracks():
    """Live capture must have NO audio filters (latency would break pause)."""
    cmd = build_ffmpeg_cmd(
        _cfg(record_screen=True, record_mic=True, record_system_audio=True), OUT, DEV)
    joined = " ".join(cmd)
    assert "x11grab" in cmd
    assert cmd.count("-f") == 3            # x11grab + 2 pulse inputs
    assert "mic_src" in cmd and "sink.monitor" in cmd
    # Two separate audio tracks, unprocessed.
    assert "-map 1:a -map 2:a" in joined
    for f in ("loudnorm", "amix", "afftdn", "highpass", "filter_complex"):
        assert f not in joined, f"{f} must not run during live capture"
    assert cmd[-1] == str(OUT)


def test_capture_startup_stall_flags():
    cmd = build_ffmpeg_cmd(_cfg(record_screen=True), OUT, DEV)
    joined = " ".join(cmd)
    assert "-thread_queue_size 1024" in joined
    assert "-tune zerolatency" in joined


def test_capture_fullscreen_offset_and_area_offset():
    assert "-i :0+0,0" in " ".join(build_ffmpeg_cmd(_cfg(record_screen=True), OUT, DEV))
    dev = CaptureDevices(display=":0", video_size="800x600", video_x=100,
                         video_y=50, mic_source="m", monitor_source="s")
    joined = " ".join(build_ffmpeg_cmd(_cfg(record_screen=True), OUT, dev))
    assert "-video_size 800x600" in joined and "-i :0+100,50" in joined


def test_capture_cursor_visibility_on_x11():
    """x11grab draws the pointer by default; -draw_mouse 0 is what hides it."""
    on = " ".join(build_ffmpeg_cmd(_cfg(record_screen=True, show_cursor=True), OUT, DEV))
    off = " ".join(build_ffmpeg_cmd(_cfg(record_screen=True, show_cursor=False), OUT, DEV))
    assert "-draw_mouse 1" in on
    assert "-draw_mouse 0" in off


def test_capture_cursor_is_not_an_ffmpeg_flag_on_wayland():
    """On Wayland the compositor owns the pointer, not ffmpeg.

    The portal decides via cursor_mode at SelectSources time, so -draw_mouse
    must not appear on the rawvideo input — it would be meaningless there.
    """
    for visible in (True, False):
        joined = " ".join(build_ffmpeg_cmd(
            _cfg(record_screen=True, show_cursor=visible), OUT, _wayland_dev()))
        assert "-draw_mouse" not in joined


def test_capture_screen_only_has_no_audio():
    cmd = build_ffmpeg_cmd(
        _cfg(record_screen=True, record_mic=False, record_system_audio=False), OUT, DEV)
    assert "-c:a" not in cmd and "-c:v" in cmd


def test_capture_audio_only_has_no_video():
    cmd = build_ffmpeg_cmd(
        _cfg(record_screen=False, record_mic=True, record_system_audio=False), OUT, DEV,
        CaptureMode.AUDIO_ONLY)
    joined = " ".join(cmd)
    assert "x11grab" not in cmd
    assert "-map 0:a" in joined            # mic becomes input 0


def test_capture_mode_controls_video_independently_of_shared_preference():
    cfg = _cfg(record_screen=False, video_source=VideoSource.AREA,
               record_mic=True, record_system_audio=False)
    audio_only = " ".join(build_ffmpeg_cmd(
        cfg, OUT, DEV, CaptureMode.AUDIO_ONLY))
    audio_video = " ".join(build_ffmpeg_cmd(
        cfg, OUT, DEV, CaptureMode.AUDIO_VIDEO))

    assert "x11grab" not in audio_only
    assert "x11grab" in audio_video


# -- stage 1 on Wayland: portal stream instead of x11grab -----------------

def _wayland_dev(size="1920x1080", fifo="/tmp/seg.fifo"):
    return CaptureDevices(display=":0", video_size=size, video_x=0, video_y=0,
                          mic_source="mic_src", monitor_source="sink.monitor",
                          video_fifo=fifo)


def test_capture_wayland_reads_rawvideo_not_x11grab():
    """With a FIFO set, video comes from the PipeWire pump, not the X display."""
    cmd = build_ffmpeg_cmd(
        _cfg(record_screen=True, record_mic=True, record_system_audio=True),
        OUT, _wayland_dev())
    joined = " ".join(cmd)
    assert "x11grab" not in cmd
    assert "-f rawvideo -pix_fmt yuv420p -framerate 30 -video_size 1920x1080" in joined
    assert "-i /tmp/seg.fifo" in joined
    assert cmd.count("-f") == 3            # rawvideo + 2 pulse inputs
    # Everything downstream of the input must be identical to the X11 path.
    assert "-map 0:v" in joined and "-map 1:a -map 2:a" in joined
    assert "-tune zerolatency" in joined
    for f in ("loudnorm", "amix", "filter_complex"):
        assert f not in joined, f"{f} must not run during live capture"


def test_pipewire_pump_matches_the_caps_ffmpeg_expects():
    """The pump must emit exactly what build_ffmpeg_cmd declares as its input."""
    cfg = _cfg(record_screen=True)
    cmd = build_pipewire_cmd(cfg, node_id=42, fd=7, size="1920x1080",
                             fifo="/tmp/seg.fifo")
    joined = " ".join(cmd)
    assert "pipewiresrc fd=7 path=42" in joined
    assert "video/x-raw,format=I420,width=1920,height=1080" in joined
    assert "framerate=30/1" in joined
    assert "filesink location=/tmp/seg.fifo" in joined
    assert "videocrop" not in joined       # no cropping for fullscreen
    # No encoder in the pump: ffmpeg owns every encode decision.
    for enc in ("x264enc", "vp8enc", "matroskamux"):
        assert enc not in joined


def test_pipewire_pump_crops_for_area_capture():
    """'area' has no portal equivalent, so the region is cropped in the pump."""
    cfg = _cfg(record_screen=True, video_source=VideoSource.AREA,
               capture_region="100,50,800,600")
    joined = " ".join(build_pipewire_cmd(cfg, 42, 7, "1920x1080", "/tmp/seg.fifo",
                                         crop=(100, 50, 800, 600)))
    assert "videocrop left=100 top=50 right=1020 bottom=430" in joined
    assert "width=800,height=600" in joined


def test_pipewire_capture_size_uses_region_then_stream():
    assert pipewire_capture_size(_cfg(video_source=VideoSource.FULLSCREEN), (1920, 1080)) == "1920x1080"
    assert pipewire_capture_size(
        _cfg(video_source=VideoSource.AREA, capture_region="0,0,800,600"), (1920, 1080)) == "800x600"
    # Rounded down to a stride-safe width (multiple of 8) and an even height:
    # x264/yuv420p needs even dimensions, and anything not a multiple of 8
    # makes GStreamer pad its rows, which shears the picture. See
    # test_pipewire_size_is_always_stride_safe.
    assert pipewire_capture_size(_cfg(video_source=VideoSource.FULLSCREEN), (1367, 769)) == "1360x768"
    # A bad region falls back to the whole stream rather than failing.
    assert pipewire_capture_size(
        _cfg(video_source=VideoSource.AREA, capture_region="bogus"), (1920, 1080)) == "1920x1080"


def test_pipewire_size_is_always_stride_safe():
    """The pump's width must be a multiple of 8, or the picture shears.

    Regression: GStreamer pads I420 rows to a 4-byte stride while ffmpeg's
    rawvideo demuxer assumes tightly packed rows. Luma needs width % 4 == 0 and
    chroma (width / 2) % 4 == 0, so only multiples of 8 satisfy both. Widths
    like 645 made GStreamer write more bytes per row than ffmpeg read, offsetting
    every row and smearing the video diagonally. 1920 and 640 happen to be safe,
    which is why full screen looked fine and a dragged region did not.
    """
    import re
    for region, stream in [("0,0,645,361", (1920, 1080)),
                           ("10,10,700,500", (1920, 1080)),
                           ("0,0,802,603", (1920, 1080)),
                           (None, (1366, 768)),      # full screen, odd width
                           (None, (1920, 1080))]:
        cfg = _cfg(record_screen=True,
                   video_source=VideoSource.AREA if region else VideoSource.FULLSCREEN,
                   capture_region=region or "")
        size = pipewire_capture_size(cfg, stream)
        width = int(size.split("x")[0])
        assert width % 8 == 0, f"{region or stream}: width {width} would shear"

        # ffmpeg is told the frame size separately from the caps the pump
        # produces; if those ever disagree the same shearing returns.
        crop = pipewire_region(cfg, stream)
        cmd = " ".join(build_pipewire_cmd(cfg, 1, 2, f"{stream[0]}x{stream[1]}",
                                          "/tmp/f", crop))
        caps = re.search(r"width=(\d+),height=(\d+)", cmd)
        assert f"{caps.group(1)}x{caps.group(2)}" == size, (
            f"{region or stream}: pump emits {caps.group(0)} but ffmpeg expects {size}")


def test_region_outside_the_screen_is_clamped():
    """A region running off the edge must be trimmed, not silently rescaled.

    Unclamped, x11grab was asked to grab past the screen and videocrop yielded
    a smaller rectangle than the caps demanded, so videoscale upscaled it into
    a blurry stretch of the wrong area.
    """
    assert clamp_region((1600, 900, 800, 400), (1920, 1080)) == (1600, 900, 320, 180)
    assert clamp_region((-50, -20, 400, 300), (1920, 1080)) == (0, 0, 400, 300)
    assert clamp_region((100, 100, 200, 200), (1920, 1080)) == (100, 100, 200, 200)
    # Entirely off-screen: nothing to record, so the caller falls back.
    assert clamp_region((3000, 2000, 100, 100), (1920, 1080)) is None


def test_audio_roles_order():
    assert audio_roles(_cfg(record_mic=True, record_system_audio=True)) == ["mic", "system"]
    assert audio_roles(_cfg(record_mic=False, record_system_audio=True)) == ["system"]
    assert audio_roles(_cfg(record_mic=True, record_system_audio=False)) == ["mic"]
    assert audio_roles(_cfg(record_mic=False, record_system_audio=False)) == []


# -- stage 2: finalize ----------------------------------------------------

def test_finalize_normalizes_both_sources_and_copies_video():
    cmd = build_finalize_cmd(
        _cfg(record_screen=True, mic_volume=1.0, system_volume=1.0,
             normalize_voice=True, noise_cancellation=True),
        LIST, OUT, ["mic", "system"])
    joined = " ".join(cmd)
    assert "-f concat -safe 0 -i /tmp/list.txt" in joined
    # Mic: highpass -> denoise -> GATE -> loudnorm -> volume.
    # The gate must come *before* loudnorm: loudnorm applies whatever gain
    # reaches -23 LUFS, so any room tone still present here gets amplified
    # with the voice (measured: the ungated chain was 9 dB louder than the
    # raw mic on a silent room).
    assert ("[0:a:0]highpass=f=90,afftdn=nr=25:nf=-35:tn=1,"
            "agate=threshold=0.02:ratio=6:attack=10:release=300,"
            "loudnorm=I=-23:TP=-2,volume=1.0[a0]") in joined
    # System: loudnorm -> volume (same target => equal voices)
    assert "[0:a:1]loudnorm=I=-23:TP=-2,volume=1.0[a1]" in joined
    assert "amix=inputs=2:normalize=0:dropout_transition=0,alimiter=limit=0.95[aout]" in joined
    # Video is copied, never re-encoded (keeps finalize fast).
    assert "-map 0:v -c:v copy" in joined
    assert cmd[-1] == str(OUT)


def test_finalize_single_mic_track_gets_limiter():
    cmd = build_finalize_cmd(_cfg(record_screen=True, mic_volume=1.0,
                                  normalize_voice=True), LIST, OUT, ["mic"])
    joined = " ".join(cmd)
    assert "amix" not in joined
    assert "alimiter=limit=0.95[aout]" in joined
    assert "-map [aout]" in joined


def test_finalize_normalization_can_be_disabled():
    cmd = build_finalize_cmd(_cfg(record_screen=True, mic_volume=2.0,
                                  system_volume=0.5, normalize_voice=False),
                             LIST, OUT, ["mic", "system"])
    joined = " ".join(cmd)
    assert "loudnorm" not in joined
    assert "volume=2.0[a0]" in joined and "volume=0.5[a1]" in joined


def test_finalize_noise_cancellation_toggle():
    on = " ".join(build_finalize_cmd(_cfg(noise_cancellation=True), LIST, OUT, ["mic"]))
    off = " ".join(build_finalize_cmd(_cfg(noise_cancellation=False), LIST, OUT, ["mic"]))
    assert "afftdn=nr=25:nf=-35:tn=1" in on
    assert "afftdn" not in off
    # The gate is part of noise cancellation, so it follows the same switch.
    assert "agate=" in on
    assert "agate" not in off
    # Gate before loudnorm, or loudnorm just amplifies what the gate would
    # have removed.
    assert on.index("agate=") < on.index("loudnorm")


def test_finalize_without_audio_just_copies_video():
    cmd = build_finalize_cmd(_cfg(record_screen=True), LIST, OUT, [])
    joined = " ".join(cmd)
    assert "-filter_complex" not in joined
    assert "-map 0:v -c:v copy" in joined


# -- geometry helpers -----------------------------------------------------

def test_parse_region():
    assert parse_region("100,50,800,600") == (100, 50, 800, 600)
    assert parse_region("") is None
    assert parse_region("1,2,3") is None
    assert parse_region("0,0,0,600") is None      # zero width rejected


def test_video_geometry_fullscreen_and_bad_area_fallback():
    x, y, size = video_geometry(_cfg(video_source=VideoSource.FULLSCREEN))
    assert (x, y) == (0, 0) and "x" in size
    x, y, size = video_geometry(_cfg(video_source=VideoSource.parse("unexpected")))
    assert (x, y) == (0, 0) and "x" in size
    x, y, size = video_geometry(_cfg(video_source=VideoSource.AREA, capture_region=""))
    assert (x, y) == (0, 0)
    x, y, size = video_geometry(_cfg(video_source=VideoSource.AREA, capture_region="10,20,801,601"))
    assert (x, y) == (10, 20) and size == "800x600"


def test_video_source_selects_fullscreen_window_and_area_on_x11():
    import meeting_recorder.recorder as rec

    previous = rec.screen_resolution, rec.active_window_geometry
    rec.screen_resolution = lambda: "1920x1080"
    rec.active_window_geometry = lambda: (20, 30, 800, 600)
    try:
        assert video_geometry(_cfg(video_source=VideoSource.FULLSCREEN)) == (0, 0, "1920x1080")
        assert video_geometry(_cfg(video_source=VideoSource.WINDOW)) == (20, 30, "800x600")
        assert video_geometry(_cfg(video_source=VideoSource.AREA, capture_region="10,20,801,601")) == (
            10, 20, "800x600")
    finally:
        rec.screen_resolution, rec.active_window_geometry = previous


# -- pause/resume segmenting ----------------------------------------------

def test_pause_resume_creates_segments_and_tracks_elapsed():
    """Pause ends a segment, resume starts a new one (no real ffmpeg)."""
    import meeting_recorder.recorder as rec

    class FakeProc:
        def __init__(self, *a, **k):
            self.stdin = None
            self._alive = True

        def poll(self):
            return None if self._alive else 0

        def wait(self, timeout=None):
            self._alive = False
            return 0

    orig = (rec.subprocess.Popen, rec.resolve_devices, rec.build_ffmpeg_cmd)
    rec.subprocess.Popen = FakeProc
    rec.resolve_devices = lambda cfg, session=None: None
    rec.build_ffmpeg_cmd = lambda cfg, out, dev, capture_mode: ["true"]
    # This test is about segment bookkeeping, not capture backends — pin the
    # backend so it behaves the same on an X11 and a Wayland machine.
    prev_backend = os.environ.get("MEETING_RECORDER_CAPTURE")
    os.environ["MEETING_RECORDER_CAPTURE"] = "x11"
    try:
        r = rec.Recorder(load_config())
        r.start(Path("/tmp/seg.mkv"), "Manual", CaptureMode.AUDIO_VIDEO)
        assert r.is_recording and not r.is_paused
        assert len(r._parts) == 1
        r.pause()
        assert r.is_paused and len(r._parts) == 1      # no new segment while paused
        r.resume()
        assert not r.is_paused and len(r._parts) == 2  # resume opened segment 1
        # Segment files are hidden siblings of the final path.
        assert r._parts[0].name == ".seg.part0.mkv"
        assert r._parts[1].name == ".seg.part1.mkv"
    finally:
        (rec.subprocess.Popen, rec.resolve_devices, rec.build_ffmpeg_cmd) = orig
        if prev_backend is None:
            os.environ.pop("MEETING_RECORDER_CAPTURE", None)
        else:
            os.environ["MEETING_RECORDER_CAPTURE"] = prev_backend


def test_finalize_drops_video_when_none_was_captured():
    """A denied screen-share must still save the audio.

    Regression: finalize mapped 0:v from cfg.record_screen even when the
    segments were audio-only, so ffmpeg failed and the whole recording was lost.
    """
    import meeting_recorder.recorder as rec

    r = rec.Recorder(_cfg(record_screen=True, record_mic=True,
                          record_system_audio=False))
    r._has_video = False                       # portal was denied
    captured = {}

    def fake_finalize(_cfg, _listfile, _dest, _roles, _duration, capture_mode):
        captured["capture_mode"] = capture_mode
        return ["true"]

    orig = rec.build_finalize_cmd
    rec.build_finalize_cmd = fake_finalize
    orig_popen = rec.subprocess.Popen
    rec.subprocess.Popen = lambda *a, **k: None
    try:
        r._start_finalize([Path("/tmp/.a.part0.mkv")], Path("/tmp/a.mkv"))
    finally:
        rec.build_finalize_cmd = orig
        rec.subprocess.Popen = orig_popen
        Path("/tmp/.a.concat.txt").unlink(missing_ok=True)
    assert captured["capture_mode"] is CaptureMode.AUDIO_ONLY


def test_portal_unavailable_records_audio_only_for_an_audio_video_request():
    """A failed portal must not turn an audio-video request into black video."""
    import meeting_recorder.recorder as rec

    class FakeProc:
        def __init__(self, *_args, **_kwargs):
            self.stdin = None

        def poll(self):
            return None

    captured = []
    original = rec.subprocess.Popen, rec.resolve_devices, rec.build_ffmpeg_cmd
    previous_backend = os.environ.get("MEETING_RECORDER_CAPTURE")
    rec.subprocess.Popen = FakeProc
    rec.resolve_devices = lambda _cfg, _session=None: DEV
    rec.build_ffmpeg_cmd = lambda _cfg, _path, _dev, capture_mode: (
        captured.append(capture_mode) or ["true"])
    os.environ["MEETING_RECORDER_CAPTURE"] = "portal"
    try:
        recorder = rec.Recorder(_cfg(record_screen=True))
        recorder.start(Path("/tmp/portal-denied.mkv"), "Manual", CaptureMode.AUDIO_VIDEO)
    finally:
        rec.subprocess.Popen, rec.resolve_devices, rec.build_ffmpeg_cmd = original
        if previous_backend is None:
            os.environ.pop("MEETING_RECORDER_CAPTURE", None)
        else:
            os.environ["MEETING_RECORDER_CAPTURE"] = previous_backend

    assert recorder._has_video is False
    assert captured == [CaptureMode.AUDIO_ONLY]


def test_finalize_trims_tail_when_duration_given():
    cmd = build_finalize_cmd(_cfg(record_screen=True), LIST, OUT, ["mic"],
                             duration=12.5)
    assert "-t 12.500" in " ".join(cmd)


def test_finalize_no_trim_by_default():
    assert "-t " not in " ".join(build_finalize_cmd(_cfg(), LIST, OUT, ["mic"]))
