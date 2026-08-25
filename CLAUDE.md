# CLAUDE.md — Smart Meeting Recorder

Context primer for AI assistants. Read this first; open source files only as needed.

## Don't

- **Never `git push` unless the maintainer explicitly asks you to.** By default the maintainer
  reviews the diff and pushes manually. Never use `--force`. Do not push to `main` unless the
  maintainer explicitly instructs you to do so.
- **Never add a `Co-Authored-By: Claude ...` trailer** (or any similar attribution line) to commit
  messages. Write the message as the maintainer would; no tool attribution.
- **Never run `sudo` without the maintainer's explicit approval.** If `sudo` is required, ask for
  permission first before executing the command.
- **Don't delete recordings, `~/.config/meeting-recorder/`, or anything under
  `~/Videos/MeetingRecorder/`** without explicit permission. Build artifacts and `/tmp` scratch are
  fine to clean.

The working loop is: make the change → verify it → **report and wait**. The maintainer reviews,
commits and pushes.

## Idea
Lightweight Linux background daemon that auto-detects online meetings/calls (Zoom, Meet,
Teams, Discord, Slack, browsers), shows a **permission popup**, and on one click records
screen + mic + optional system audio — stopping automatically when the call ends. Privacy-first:
never records without consent.

## Concept / how it works
- **Detection = microphone capture by a known app.** PipeWire/PulseAudio exposes each active
  mic stream as a *source-output* tagged with the owning app. We poll `pactl list source-outputs`
  and match `application.name`/`binary` against an editable allowlist. Monitor (system-audio)
  streams are ignored so only real mic use triggers.
- **Debounce** avoids flapping: ~3s sustained presence to start, ~3s absence to stop.
  Muting yourself makes the app release the mic stream, which is indistinguishable
  from leaving the call — so `stop_debounce_seconds` is also the longest mute that
  will not end the recording. Raising it costs nothing in the file (the wait is
  reported as `stop_overshoot` and trimmed off the end) but does leave the recorder
  visibly running after the call.
- **Popup** via libnotify (`gi.repository.Notify`) with Record/Ignore buttons.
- **Recording is two-stage** (this is the core design — don't put audio filters back in the live path):
  1. *Live capture*: video + mic and system audio as **separate, unfiltered** tracks.
     No filters ⇒ no latency ⇒ segments can be any length.
  2. *Finalize* (`build_finalize_cmd`): concat segments, then denoise + per-source `loudnorm`
     + `amix` + limiter, **video stream-copied**. Runs as a **background** Popen polled from the
     GLib loop (loudnorm is only ~45x realtime ⇒ ~4.6 min for a 1h meeting); daemon stays responsive.
- **Pause/resume** ends/starts a segment, so paused time never reaches the file.
  (`SIGSTOP` does NOT work — input queues keep buffering and the paused span gets encoded on resume.)
- **Why loudnorm can't run live**: it buffers ~3s, which (a) silently truncated the last ~3s of every
  recording and (b) makes a clean pause boundary impossible. Output `.mkv` named `<App>_<timestamp>`.
- **Two capture backends, one ffmpeg** (`screencast.use_portal_capture()` picks; override with
  `MEETING_RECORDER_CAPTURE=portal|x11`):
  - *X11*: `-f x11grab -i $DISPLAY+x,y`, geometry from xrandr/xprop/xwininfo.
  - *Wayland*: the compositor owns the screen, so we ask `xdg-desktop-portal`'s ScreenCast API for
    a PipeWire node. **ffmpeg has no PipeWire input device**, so a `gst-launch-1.0 pipewiresrc`
    pump (`build_pipewire_cmd`) converts to raw I420 and writes it into a **FIFO** that ffmpeg
    reads as `-f rawvideo`. The pump does *no* encoding — ffmpeg still owns every encode decision,
    keeps one clock for A/V, and `build_finalize_cmd` is untouched.
    - It must be a FIFO, **not stdin**: `_stop_proc` writes `q` to ffmpeg's stdin to end a segment.
    - The pump writes the FIFO itself (`filesink`) so the open-blocks-until-both-ends wait happens
      in the child, never in the daemon's GLib loop.
    - The portal handshake (`CreateSession`→`SelectSources`→`Start`) is **async** and needs the
      GLib loop; `persist_mode=2` returns a `restore_token` (stored in config) so the user is only
      prompted once. `OpenPipeWireRemote` is sync and gives a fresh fd per segment, so pause/resume
      never re-prompts.
    - No portal permission ⇒ **audio-only**, never a silently-black video.
- **Startup stall fix**: `-tune zerolatency` + `-thread_queue_size 1024` (otherwise x11grab starves
  and the first ~3s is a frozen black screen).
- Everything runs on one **GLib main loop**; ships as a `systemctl --user` service.

## Environment assumptions
Source use is supported on Ubuntu 24.04. GitHub releases publish the runtime
image to GHCR under the policy in [`docs/CONTAINER-IMAGES.md`](docs/CONTAINER-IMAGES.md).
The desktop runtime uses **X11 or Wayland** (X11 is the recommended/steadier path; the
Wayland portal is an extra moving part and can crash mid-session), GNOME, **PipeWire**. Runs on system `python3` using system
PyGObject — **no pip dependencies**. Requires apt: `ffmpeg`, `pulseaudio-utils`,
`gir1.2-notify-0.7`; `xdg-desktop-portal` + `gstreamer1.0-pipewire` for Wayland capture,
`x11-xserver-utils` + `x11-utils` for X11 capture.

Wayland caveats: `Gtk.Window.move()` is a no-op (the compositor places the control pill) and
`slop` region-select is X11-only.

## Structure (`meeting_recorder/`)
| File | Responsibility |
|------|----------------|
| `__main__.py` | CLI (`run`/`record`/`status`/`config`); wires everything on the GLib loop |
| `config.py` | Load `config/default_config.json` + user `~/.config/meeting-recorder/config.json` |
| `detector.py` | **Core.** Parse pactl, allowlist match, `DebounceMachine`, `MeetingDetector` poller |
| `audio.py` | Resolve default mic source + sink monitor via pactl |
| `recorder.py` | `build_ffmpeg_cmd` / `build_pipewire_cmd` (pure) + `Recorder` process lifecycle |
| `screencast.py` | Wayland: `use_portal_capture()`, ScreenCast portal D-Bus handshake, PipeWire fd |
| `notifier.py` | libnotify popup with action buttons (+ `notify-send` fallback); info notifications auto-dismiss ~2s |
| `recording_widget.py` | Floating top-right GTK3 pill: blinking dot, timer, pause/resume/stop |
| `settings_gui.py` | GTK3 settings window (`settings` subcommand); edits user config |
| `controller.py` | State machine IDLE→PROMPTING→RECORDING→IDLE; owns the widget + elapsed timer |
| `utils.py` | paths, filenames, logging |

`tests/` — zero-dep runner: `python3 tests/run_tests.py` (no pytest needed). Pure functions
(`parse_source_outputs`, `match_meeting_app`, `DebounceMachine`, `build_ffmpeg_cmd`,
`build_pipewire_cmd`) are the
unit-test surface. `systemd/` contains the user-service assets.

## Conventions
- Keep it dependency-free (stdlib + system `gi` only). No pip packages.
- Detection/recording logic must stay in pure, testable functions; subprocess/GLib at the edges.
- Config is flat JSON; add keys to `default_config.json` and `Config` together.
- Never record without an explicit user Record click unless `auto_record` is set.
