"""Screen + audio recording via ffmpeg.

Two-stage design:

1. **Live capture** (`build_ffmpeg_cmd`) writes a segment with the video plus the
   mic and system audio as two *separate, unprocessed* tracks. No audio filters
   run live, so there is no filter latency — segments can be any length and
   nothing is lost when one ends.
2. **Finalize** (`build_finalize_cmd`) concatenates the segments and does all the
   audio work in one pass: denoise, per-source loudness normalization (so both
   voices end up equal), mixing and limiting. The video is stream-copied, so this
   is fast even for a long meeting.

That split is what makes Pause exact: pausing ends a segment and resuming starts
a new one, so paused time never reaches the file. (Normalizing live is not an
option — loudnorm buffers ~3s, which both truncates recordings and makes a clean
pause boundary impossible.)

The command builders are pure functions and are unit-tested; `Recorder` owns the
subprocess lifecycle.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import audio
from .config import Config
from .domain import CaptureMode, CompletedRecording, VideoSource
from .utils import LOG, expand_path

_LIMITER = "alimiter=limit=0.95"
_LOUDNORM = "loudnorm=I=-23:TP=-2"
# threshold=0.02 is about -34 dBFS: well under speech, above room tone. The
# slow release avoids the background "pumping" audibly between words.
_GATE = "agate=threshold=0.02:ratio=6:attack=10:release=300"


def screen_resolution(default: str = "1920x1080") -> str:
    """Current X11 resolution via xrandr, e.g. '1920x1080'. Falls back on failure."""
    try:
        out = subprocess.run(["xrandr", "--current"], capture_output=True,
                             text=True, timeout=5, check=True)
        for line in out.stdout.splitlines():
            if "*" in line:  # the active mode is starred
                return line.split()[0]
    except (subprocess.SubprocessError, FileNotFoundError, IndexError) as exc:
        LOG.debug("xrandr failed, using default resolution: %s", exc)
    return default


def _even(n: int) -> int:
    """x264/yuv420p needs even dimensions."""
    return n - (n % 2)


def _packed_width(n: int) -> int:
    """Round down to a width GStreamer emits without row padding.

    GStreamer pads I420 rows to a 4-byte stride, but ffmpeg's rawvideo demuxer
    assumes rows are tightly packed. When they disagree every row is offset by
    a few bytes and the picture shears into diagonal colour smears. Luma needs
    width % 4 == 0 and chroma needs (width / 2) % 4 == 0, so a multiple of 8
    satisfies both. 1920 and 640 already do, which is why full-screen capture
    looked fine while an arbitrary dragged region did not.
    """
    return n - (n % 8)


def active_window_geometry() -> tuple[int, int, int, int] | None:
    """(x, y, w, h) of the currently focused window via xprop + xwininfo."""
    try:
        wid_out = subprocess.run(["xprop", "-root", "_NET_ACTIVE_WINDOW"],
                                 capture_output=True, text=True, timeout=3, check=True)
        wid = wid_out.stdout.split()[-1]
        if wid in ("0x0", "0x00000000"):
            return None
        info = subprocess.run(["xwininfo", "-id", wid], capture_output=True,
                              text=True, timeout=3, check=True).stdout
        x = int(re.search(r"Absolute upper-left X:\s*(-?\d+)", info).group(1))
        y = int(re.search(r"Absolute upper-left Y:\s*(-?\d+)", info).group(1))
        w = int(re.search(r"Width:\s*(\d+)", info).group(1))
        h = int(re.search(r"Height:\s*(\d+)", info).group(1))
        return x, y, w, h
    except (subprocess.SubprocessError, FileNotFoundError, AttributeError,
            ValueError, IndexError) as exc:
        LOG.warning("Could not read active window geometry: %s", exc)
        return None


def parse_region(text: str) -> tuple[int, int, int, int] | None:
    """Parse a 'x,y,w,h' region string into ints, or None if invalid."""
    try:
        x, y, w, h = (int(p) for p in text.split(","))
        if w > 0 and h > 0:
            return x, y, w, h
    except (ValueError, AttributeError):
        pass
    return None


def clamp_region(geo: tuple[int, int, int, int],
                 bounds: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """Trim a region to the screen, or None if it lies entirely outside it.

    A region can extend past the edge — a drag that ran off-screen, a saved
    region from a larger monitor, or a hand-typed one. Left unclamped it
    silently produced the wrong picture rather than an error: x11grab is asked
    to grab off-screen, and on Wayland videocrop yields a smaller rectangle
    than the caps demand, so videoscale upscales it into a blurry stretch.
    """
    x, y, w, h = geo
    max_w, max_h = bounds
    x = max(0, min(x, max_w))
    y = max(0, min(y, max_h))
    w = min(w, max_w - x)
    h = min(h, max_h - y)
    if w <= 0 or h <= 0:
        return None
    return x, y, w, h


def video_geometry(cfg: Config) -> tuple[int, int, str]:
    """Resolve (x_offset, y_offset, 'WxH') for the chosen video source.

    Falls back to full screen if a window/area can't be determined.
    """
    if cfg.video_source is VideoSource.WINDOW:
        geo = active_window_geometry()
        if geo:
            x, y, w, h = geo
            return x, y, f"{_even(w)}x{_even(h)}"
        LOG.warning("Window capture unavailable; using full screen")
    elif cfg.video_source is VideoSource.AREA:
        geo = parse_region(cfg.capture_region)
        if geo:
            size = screen_resolution()
            bounds = tuple(int(p) for p in size.split("x"))
            clamped = clamp_region(geo, bounds)
            if clamped is None:
                LOG.warning("capture_region %s is off-screen; using full screen",
                            cfg.capture_region)
                return 0, 0, size
            if clamped != geo:
                LOG.info("capture_region trimmed to the screen: %s", clamped)
            x, y, w, h = clamped
            return x, y, f"{_even(w)}x{_even(h)}"
        LOG.warning("No valid capture_region; using full screen")
    return 0, 0, screen_resolution()


@dataclass
class CaptureDevices:
    display: str          # e.g. ":0"
    video_size: str       # "WxH" of the capture rectangle
    video_x: int          # left offset on the X display
    video_y: int          # top offset on the X display
    mic_source: str
    monitor_source: str
    # Wayland only: FIFO carrying raw frames from the PipeWire pump. When set,
    # ffmpeg reads rawvideo from it instead of grabbing the X display.
    video_fifo: str | None = None


def resolve_devices(cfg: Config, session=None) -> CaptureDevices:
    """Resolve the capture sources for one segment.

    With a `session` (an open `screencast.ScreenCastSession`) the video comes
    from the portal's PipeWire stream and its size is whatever the compositor
    gave us; otherwise this is the X11 path and geometry comes from xrandr.
    """
    if session is not None and session.size:
        x, y, size = 0, 0, pipewire_capture_size(cfg, session.size)
    else:
        x, y, size = video_geometry(cfg)
    return CaptureDevices(
        display=os.environ.get("DISPLAY", ":0"),
        video_size=size,
        video_x=x,
        video_y=y,
        mic_source=audio.default_source(),
        monitor_source=audio.monitor_source(),
    )


def build_pipewire_cmd(cfg: Config, node_id: int, fd: int, size: str,
                       fifo: Path | str,
                       crop: tuple[int, int, int, int] | None = None) -> list[str]:
    """GStreamer pipeline pumping a portal stream into `fifo` as raw I420.

    This exists only because ffmpeg has no PipeWire input device. It does no
    encoding — it converts to exactly the caps `build_ffmpeg_cmd` declares for
    its rawvideo input, so ffmpeg still owns every encode decision.

    The pipeline writes to the FIFO itself rather than to a pipe we hold, so
    the open-blocks-until-both-ends-are-there wait happens in this child and
    never in the daemon's main loop.

    `crop` is (x, y, w, h) for "area" capture: the portal always hands over a
    whole monitor or window, so the region is trimmed here instead.
    """
    stream_w, stream_h = (int(p) for p in size.split("x"))
    if crop:
        width, height = _packed_width(crop[2]), _even(crop[3])
    else:
        width, height = _packed_width(stream_w), _even(stream_h)
    pipeline = [
        "pipewiresrc", f"fd={fd}", f"path={node_id}", "do-timestamp=true",
        "!", "videorate",
        "!", f"video/x-raw,framerate={cfg.framerate}/1",
    ]
    if crop:
        cx, cy, cw, ch = crop
        pipeline += ["!", "videocrop", f"left={cx}", f"top={cy}",
                     f"right={max(0, stream_w - cx - cw)}",
                     f"bottom={max(0, stream_h - cy - ch)}"]
    pipeline += [
        "!", "videoconvert", "!", "videoscale",
        "!", f"video/x-raw,format=I420,width={width},height={height}",
        "!", "filesink", f"location={fifo}", "sync=false",
    ]
    return ["gst-launch-1.0", "-q"] + pipeline


def pipewire_region(cfg: Config,
                    session_size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """The crop rectangle for "area" capture, clamped to the portal stream."""
    if cfg.video_source is not VideoSource.AREA:
        return None
    geo = parse_region(cfg.capture_region)
    if not geo:
        return None
    clamped = clamp_region(geo, session_size)
    if clamped is None:
        LOG.warning("capture_region %s is outside the stream; capturing all of it",
                    cfg.capture_region)
    elif clamped != geo:
        LOG.info("capture_region trimmed to the stream: %s", clamped)
    return clamped


def pipewire_output_size(cfg: Config,
                         session_size: tuple[int, int]) -> tuple[int, int]:
    """Exact (width, height) the pump emits and ffmpeg is told to expect.

    Both sides must agree to the pixel, so this is the single place the frame
    size is decided.
    """
    geo = pipewire_region(cfg, session_size)
    if geo:
        w, h = geo[2], geo[3]
    else:
        if cfg.video_source is VideoSource.AREA:
            LOG.warning("No valid capture_region; using the full portal stream")
        w, h = session_size
    return _packed_width(w), _even(h)


def pipewire_capture_size(cfg: Config, session_size: tuple[int, int]) -> str:
    """The 'WxH' ffmpeg will receive: the stream size, or the cropped region."""
    w, h = pipewire_output_size(cfg, session_size)
    return f"{w}x{h}"


def audio_roles(cfg: Config) -> list[str]:
    """Audio track roles, in the order they are recorded into each segment."""
    roles = []
    if cfg.record_mic:
        roles.append("mic")
    if cfg.record_system_audio:
        roles.append("system")
    return roles


# ---------------------------------------------------------------------------
# Stage 1: live capture (no audio filters -> no latency)
# ---------------------------------------------------------------------------


def build_ffmpeg_cmd(cfg: Config, output_path: Path, dev: CaptureDevices,
                     capture_mode: CaptureMode = CaptureMode.AUDIO_VIDEO) -> list[str]:
    """Live capture: video + each audio source as its own untouched track."""
    cmd: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y"]

    # Large input queues so a busy encoder never starves screen capture (this is
    # what prevents a frozen black screen at the start of a recording).
    tqs = ["-thread_queue_size", "1024"]
    next_index = 0

    if capture_mode is CaptureMode.AUDIO_VIDEO:
        if dev.video_fifo:
            # Wayland: frames arrive as raw I420 from the PipeWire pump. ffmpeg
            # cannot read PipeWire itself, so the pump does that part.
            cmd += tqs + ["-f", "rawvideo", "-pix_fmt", "yuv420p",
                          "-framerate", str(cfg.framerate),
                          "-video_size", dev.video_size,
                          "-i", dev.video_fifo]
        else:
            cmd += tqs + ["-f", "x11grab", "-framerate", str(cfg.framerate),
                          "-draw_mouse", "1" if cfg.show_cursor else "0",
                          "-video_size", dev.video_size,
                          "-i", f"{dev.display}+{dev.video_x},{dev.video_y}"]
        next_index += 1  # video occupies input 0

    audio_indices: list[int] = []
    if cfg.record_mic:
        cmd += tqs + ["-f", "pulse", "-i", dev.mic_source]
        audio_indices.append(next_index)
        next_index += 1
    if cfg.record_system_audio:
        cmd += tqs + ["-f", "pulse", "-i", dev.monitor_source]
        audio_indices.append(next_index)
        next_index += 1

    # 'zerolatency' disables x264 lookahead/B-frames so frames are emitted
    # immediately (no startup stall); -g sets a ~2s keyframe interval.
    if capture_mode is CaptureMode.AUDIO_VIDEO:
        cmd += ["-map", "0:v", "-c:v", cfg.video_codec,
                "-preset", cfg.video_preset, "-pix_fmt", "yuv420p",
                "-g", str(cfg.framerate * 2)]
        if "264" in cfg.video_codec or "265" in cfg.video_codec:
            cmd += ["-tune", "zerolatency"]

    for idx in audio_indices:
        cmd += ["-map", f"{idx}:a"]
    if audio_indices:
        cmd += ["-c:a", "aac", "-b:a", "160k"]

    cmd.append(str(output_path))
    return cmd


# ---------------------------------------------------------------------------
# Stage 2: finalize (all the audio processing, video stream-copied)
# ---------------------------------------------------------------------------


def _mic_chain(cfg: Config) -> list[str]:
    chain = ["highpass=f=90"]
    if cfg.noise_cancellation:
        model = expand_path(cfg.noise_model_path) if cfg.noise_model_path else None
        if model and model.is_file():
            chain.append(f"arnndn=m={model}")
        else:
            chain.append("afftdn=nr=25:nf=-35:tn=1")
        # Gate the room tone that survives denoising, and do it *before*
        # loudnorm. Order matters more than strength here: loudnorm applies
        # whatever gain reaches -23 LUFS, so anything still audible at this
        # point gets amplified along with the voice — measured on a silent
        # room, the old chain ended up 9 dB *louder* than the raw mic.
        # Soft-knee (ratio 6, not infinite) so quiet speech ducks rather than
        # being chopped off mid-word.
        chain.append(_GATE)
    if cfg.normalize_voice:
        chain.append(_LOUDNORM)
    chain.append(f"volume={cfg.mic_volume}")
    return chain


def _system_chain(cfg: Config) -> list[str]:
    chain: list[str] = []
    if cfg.normalize_voice:
        chain.append(_LOUDNORM)
    chain.append(f"volume={cfg.system_volume}")
    return chain


def _chain_for(cfg: Config, role: str) -> list[str]:
    return _mic_chain(cfg) if role == "mic" else _system_chain(cfg)


def build_finalize_cmd(cfg: Config, listfile: Path, dest: Path,
                       roles: list[str], duration: float | None = None,
                       capture_mode: CaptureMode = CaptureMode.AUDIO_VIDEO) -> list[str]:
    """Concat segments, process audio, copy video. Both sources are normalized to
    the same loudness target so the two voices come out equal.

    `duration` caps the output length — used to trim the tail that was recorded
    while the detector waited out its stop debounce.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
           "-f", "concat", "-safe", "0", "-i", str(listfile)]
    if duration is not None and duration > 0:
        cmd += ["-t", f"{duration:.3f}"]

    if roles:
        if len(roles) == 1:
            chain = _chain_for(cfg, roles[0]) + [_LIMITER]
            graph = "[0:a:0]" + ",".join(chain) + "[aout]"
        else:
            stages = ["[0:a:%d]" % n + ",".join(_chain_for(cfg, r)) + f"[a{n}]"
                      for n, r in enumerate(roles)]
            labels = "".join(f"[a{n}]" for n in range(len(roles)))
            mix = f"amix=inputs={len(roles)}:normalize=0:dropout_transition=0"
            graph = ";".join(stages) + ";" + labels + f"{mix},{_LIMITER}[aout]"
        cmd += ["-filter_complex", graph, "-map", "[aout]",
                "-c:a", "aac", "-b:a", "160k"]

    if capture_mode is CaptureMode.AUDIO_VIDEO:
        cmd += ["-map", "0:v", "-c:v", "copy"]

    cmd.append(str(dest))
    return cmd


class FinalizationHandle:
    """Owns one detached finalization process and its immutable run snapshot."""

    def __init__(self, proc: subprocess.Popen | None, target: Path | None,
                 parts: list[Path], listfile: Path | None,
                 source_app: str, requested_mode: CaptureMode, has_audio: bool,
                 has_video: bool, started_at: datetime, ended_at: datetime):
        self._proc = proc
        self._target, self._parts, self._listfile = target, parts, listfile
        self._source_app, self._requested_mode = source_app, requested_mode
        self._has_audio, self._has_video = has_audio, has_video
        self._started_at, self._ended_at = started_at, ended_at
        self._result: CompletedRecording | None = None
        self._complete = proc is None
        if self._complete:
            self._cleanup()

    def _cleanup(self) -> None:
        for part in self._parts:
            part.unlink(missing_ok=True)
        if self._listfile:
            self._listfile.unlink(missing_ok=True)
        self._parts = []
        self._listfile = None

    @property
    def target_path(self) -> Path | None:
        """The reserved final path for this one detached finalization."""
        return self._target

    def _finish(self, success: bool) -> tuple[bool, CompletedRecording | None]:
        if not self._complete:
            target = self._target
            if success and target and target.exists() and target.stat().st_size > 0:
                self._result = CompletedRecording(
                    target, self._source_app, self._requested_mode, self._has_audio,
                    self._has_video, self._started_at, self._ended_at)
            self._cleanup()
            self._complete = True
            self._proc = None
        return True, self._result

    def poll(self) -> tuple[bool, CompletedRecording | None]:
        if self._complete:
            return True, self._result
        if self._proc.poll() is None:
            return False, None
        return self._finish(self._proc.returncode == 0)

    def wait(self, timeout: float = 900) -> CompletedRecording | None:
        if self._complete:
            return self._result
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            LOG.error("Finalize timed out; killing")
            self._proc.kill()
            self._proc.wait()
            return self._finish(False)[1]
        return self._finish(self._proc.returncode == 0)[1]


class Recorder:
    """Records as one or more segments; pause ends a segment, resume starts one.

    On stop the segments are concatenated and the audio is processed in a single
    finalize pass (video stream-copied), so paused time never reaches the file.
    """

    def __init__(self, cfg: Config, session=None,
                 clock: Callable[[], datetime] | None = None):
        self.cfg = cfg
        # Wayland: an open screencast.ScreenCastSession supplying the video.
        # None on X11, where ffmpeg grabs the display directly.
        self._session = session
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._source_app: str | None = None
        self._capture_started_at: datetime | None = None
        self._requested_capture_mode: CaptureMode | None = None
        self._proc: subprocess.Popen | None = None
        self._pump: subprocess.Popen | None = None
        self._fifo: Path | None = None
        # Whether this run's segments actually contain a video stream. Decided
        # by the first segment and then held for the rest of the run: concat
        # needs every segment to have the same layout, and finalize must map
        # only streams that are really there.
        self._has_video: bool | None = None
        self._final_path: Path | None = None
        self._parts: list[Path] = []
        self._accum: float = 0.0        # active seconds from finished segments
        self._run_start: float = 0.0    # monotonic start of the live segment
        self._paused: bool = False

    @property
    def is_recording(self) -> bool:
        """True for the whole session, including while paused."""
        return self._final_path is not None

    @property
    def is_paused(self) -> bool:
        return self._paused

    def start(self, output_path: Path, source_app: str,
              capture_mode: CaptureMode) -> bool:
        if self.is_recording:
            LOG.warning("start() ignored: already recording")
            return False
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            self._final_path, self._parts, self._accum = output_path, [], 0.0
            self._paused, self._requested_capture_mode = False, capture_mode
            self._source_app, self._capture_started_at, self._has_video = source_app, None, None
            LOG.info("Recording -> %s", output_path)
            if self._start_segment():
                return True
        except (OSError, subprocess.SubprocessError) as exc:
            LOG.error("Could not start capture: %s", exc)
        self._cleanup_pump()
        self._reset_active()
        return False

    def pause(self) -> None:
        if not self.is_recording or self._paused:
            return
        self._accum += time.monotonic() - self._run_start
        self._stop_proc()
        self._paused = True
        LOG.info("Recording paused (%.0fs recorded)", self._accum)

    def resume(self) -> None:
        if not self.is_recording or not self._paused:
            return
        self._paused = False
        if not self._start_segment():
            self._paused = True
        LOG.info("Recording resumed")

    def stop(self, discard: bool = False, trim_end: float = 0.0) -> FinalizationHandle | None:
        """Stop capture and kick off the finalize pass in the background.

        `trim_end` drops that many seconds from the end of the saved file — the
        detector uses it to remove the tail recorded during its stop debounce.
        Returns a per-run finalization handle after detaching active capture state.
        """
        if not self.is_recording:
            return None
        # Snapshot wall-clock capture end before process teardown can block, then
        # detach the run so finalization cannot race a newly started recording.
        stopped_at = self._clock()
        if not self._paused:
            self._accum += time.monotonic() - self._run_start
        self._stop_proc()
        final, parts = self._final_path, self._parts
        started = self._capture_started_at or self._clock()
        ended = max(started, stopped_at - timedelta(seconds=trim_end))
        source_app = self._source_app or "Meeting"
        requested = self._requested_capture_mode or CaptureMode.AUDIO_ONLY
        has_audio = bool(self.cfg.record_mic or self.cfg.record_system_audio)
        has_video = bool(self._has_video)
        self._reset_active()

        parts = [p for p in parts if p.exists() and p.stat().st_size > 0]
        if discard or not parts or final is None:
            if not parts:
                LOG.warning("Recording stopped but no output was produced")
            for p in parts:
                p.unlink(missing_ok=True)
            return FinalizationHandle(None, final, parts, None, source_app, requested,
                                      has_audio, has_video, started, ended)
        duration = None
        if trim_end > 0:
            duration = max(0.5, self._accum - trim_end)
            LOG.info("Trimming %.1fs of post-call tail (keeping %.1fs)",
                     trim_end, duration)
        try:
            return self._start_finalize(parts, final, duration, source_app, requested,
                                        has_audio, has_video, started, ended)
        except OSError as exc:
            LOG.error("Could not start finalize: %s", exc)
            for p in parts:
                p.unlink(missing_ok=True)
            return FinalizationHandle(None, final, parts, None, source_app, requested,
                                      has_audio, has_video, started, ended)

    def elapsed(self) -> float:
        """Total active recorded seconds, excluding paused time."""
        if not self.is_recording or self._paused:
            return self._accum
        return self._accum + (time.monotonic() - self._run_start)

    def _reset_active(self) -> None:
        """Detach all capture-only state before a finalizer can outlive this run."""
        self._final_path = None
        self._parts = []
        self._paused = False
        self._proc = None
        self._requested_capture_mode = None
        self._source_app = None
        self._capture_started_at = None
        self._has_video = None

    # -- internals ---------------------------------------------------------
    def _start_segment(self) -> bool:
        assert self._final_path is not None
        assert self._requested_capture_mode is not None
        part = self._final_path.with_name(
            f".{self._final_path.stem}.part{len(self._parts)}{self._final_path.suffix}")
        dev = resolve_devices(self.cfg, self._session)
        requested_mode = self._requested_capture_mode

        # Try PipeWire before locking the first segment's actual video layout;
        # concat requires later segments to match, so derive its effective mode here.
        if self._wants_pipewire and self._has_video is not False:
            self._start_pump(part, dev)
            if not dev.video_fifo:
                # Wayland with no working pump: x11grab would capture nothing,
                # so keep the audio rather than writing a black video.
                LOG.warning("Falling back to audio-only for this recording")
        if self._has_video is None:
            self._has_video = bool(requested_mode is CaptureMode.AUDIO_VIDEO and
                                   (not self._wants_pipewire or dev.video_fifo))
        capture_mode = (CaptureMode.AUDIO_VIDEO if self._has_video
                        else CaptureMode.AUDIO_ONLY)
        cmd = build_ffmpeg_cmd(self.cfg, part, dev, capture_mode)
        LOG.debug("capture cmd: %s", " ".join(cmd))
        try:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            LOG.error("Could not start capture: %s", exc)
            self._cleanup_pump()
            return False
        self._parts.append(part)
        self._run_start = time.monotonic()
        if self._capture_started_at is None:
            self._capture_started_at = self._clock()
        return True

    @property
    def _wants_pipewire(self) -> bool:
        """True when video must come from the portal rather than x11grab.

        Keyed on the session type, not on whether a session exists: if the
        portal was denied there is still no display to grab, so the segment has
        to degrade to audio-only instead of silently recording a black screen.
        """
        from .screencast import use_portal_capture
        return bool(self._requested_capture_mode is CaptureMode.AUDIO_VIDEO and
                    use_portal_capture())

    def attach_session(self, session) -> None:
        """Supply the open ScreenCastSession that video will be pumped from."""
        self._session = session

    def _start_pump(self, part: Path, dev: CaptureDevices) -> None:
        """Start the GStreamer PipeWire->FIFO pump for one Wayland segment.

        A fresh fd per segment: OpenPipeWireRemote is a plain call with no
        dialog, so resuming after a pause costs nothing and asks nothing.
        """
        from .screencast import ScreenCastError

        if self._session is None or not self._session.is_open:
            LOG.warning("No screen-capture permission; recording audio only")
            return
        fifo = part.with_suffix(".fifo")
        fifo.unlink(missing_ok=True)
        try:
            os.mkfifo(fifo, 0o600)
            fd = self._session.open_fd()
        except (OSError, ScreenCastError) as exc:
            LOG.error("Could not set up Wayland capture: %s", exc)
            fifo.unlink(missing_ok=True)
            return
        self._fifo = fifo
        dev.video_fifo = str(fifo)

        crop = pipewire_region(self.cfg, self._session.size)
        cmd = build_pipewire_cmd(self.cfg, self._session.node_id, fd,
                                 f"{self._session.size[0]}x{self._session.size[1]}",
                                 fifo, crop)
        LOG.debug("pipewire pump cmd: %s", " ".join(cmd))
        try:
            os.set_inheritable(fd, True)
            self._pump = subprocess.Popen(
                cmd, pass_fds=(fd,),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            LOG.error("Could not start the PipeWire pump: %s", exc)
            dev.video_fifo = None
            self._cleanup_pump()
        finally:
            os.close(fd)  # the child holds its own copy now

    def _stop_proc(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.write(b"q")
                    proc.stdin.flush()
                    proc.stdin.close()
                proc.wait(timeout=8)
            except (subprocess.TimeoutExpired, BrokenPipeError, OSError):
                LOG.warning("ffmpeg did not quit cleanly; terminating")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        # Only now: the pump dies of SIGPIPE once ffmpeg drops the FIFO anyway,
        # but stopping it first would truncate the tail of the segment.
        self._cleanup_pump()

    def _cleanup_pump(self) -> None:
        pump, self._pump = self._pump, None
        if pump is not None and pump.poll() is None:
            pump.terminate()
            try:
                pump.wait(timeout=5)
            except subprocess.TimeoutExpired:
                LOG.warning("PipeWire pump did not exit; killing")
                pump.kill()
        if self._fifo is not None:
            self._fifo.unlink(missing_ok=True)
            self._fifo = None

    def _start_finalize(self, parts: list[Path], dest: Path,
                        duration: float | None = None, source_app: str = "Meeting",
                        requested: CaptureMode = CaptureMode.AUDIO_ONLY,
                        has_audio: bool = False, has_video: bool = False,
                        started: datetime | None = None,
                        ended: datetime | None = None) -> FinalizationHandle:
        listfile = dest.with_name(f".{dest.stem}.concat.txt")
        listfile.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts),
                            encoding="utf-8")
        # Map only what the segments really contain: if screen capture was
        # never granted, asking for 0:v here would fail the whole finalize and
        # throw away a perfectly good audio recording.
        capture_mode = CaptureMode.AUDIO_VIDEO if has_video else CaptureMode.AUDIO_ONLY
        if capture_mode is CaptureMode.AUDIO_ONLY:
            LOG.warning("No video was captured; saving audio only")
        cfg = self.cfg
        cmd = build_finalize_cmd(cfg, listfile, dest, audio_roles(cfg), duration,
                                 capture_mode)
        LOG.info("Finalizing %d segment(s) -> %s", len(parts), dest.name)
        LOG.debug("finalize cmd: %s", " ".join(cmd))
        started = started or self._clock()
        ended = ended or started
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            LOG.error("Could not start finalize: %s", exc)
            return FinalizationHandle(None, dest, parts, listfile, source_app, requested,
                                      has_audio, has_video, started, ended)
        return FinalizationHandle(proc, dest, parts, listfile, source_app, requested,
                                  has_audio, has_video, started, ended)
