# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions use
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- #31 shows a visible audio-only fallback notice when requested screen capture is unavailable.
- #32 uses the six-argument Calendar OpenURI response callback expected by Gio signal delivery.
- #33 makes manual portal controller coverage deterministic for Wayland and portal-ready video capture.

## [0.4.1] — 2026-08-26

### Fixed
- Mutable `latest` promotion now separates policy-approved tag movement from
  immutable alias creation.

### Changed
- Bluefin deployment now moves from the bootstrap version tag to the verified
  v0.4.0 digest.

## [0.4.0] — 2026-08-26

### Added
- #19 adds bounded Google Calendar Desktop OAuth credential management with
  PKCE, Secret Service refresh-token storage, and `calendar connect|status|disconnect`.
- #20 adds explicit Calendar selection, bounded private offline snapshots, and
  background refresh independent from recording.
- #21 adds cache-only recording enrichment, versioned `.meeting.json` sidecars,
  collision-safe Calendar naming, and `calendar correct` for offline corrections.
- #22 adds the explicit `speakr upload PATH` command with public-only durable
  publication progress, one protected private filesystem locator for operational
  path state, and environment-only Speakr token handling.
- #24 adds policy-controlled Speakr publication with disabled, manual, and
  automatic modes, exact SSID admission, and an isolated background worker;
  explicit commands remain available in every mode.
- #25 adds explicit CLI-only cleanup for old fully published Recording groups:
  preview by default, irreversible `--delete`, strict media/sidecar/path checks,
  durable intent and quarantine resumption, and retained `local_removed`
  publication history.
- #26 adds the Ubuntu 24.04 amd64 runtime-image contract, direct source
  execution, local build/smoke guidance, and explicit boundaries for host
  services, deployment, publishing, and live validation.
- #27 adds the intended GHCR container-release contract: protected manual tags,
  immutable version/SHA tags, stable-only `latest`, provenance/SBOM, and
  digest-qualified consumption after anonymous release verification.
- #28 ships Bluefin runtime prerequisites and bootstrap Quadlet/operator guidance
  for Wayland and best-effort X11, fixed secret-file Speakr tokens,
  loopback-bound container Calendar OAuth through the session-bus OpenURI portal,
  and managed restart after settings save. The final digest pin is applied after
  publication.

### Changed
- Debian/APT packaging and publishing support was removed. Releases distribute
  source and publish the runtime image to GHCR.
- Detected meetings now ask directly whether to record Video, Audio only, or
  Ignore. Closing or leaving the prompt unanswered never starts a recording,
  while auto-record continues to start audio-only without prompting.
- Recording finalization now completes independently for each run, so a new
  recording can begin while an earlier file is still being finalized.
- Video source now separately selects fullscreen, the current window, or an
  area, while Capture mode describes the media composition for each recording.
- Existing `capture_mode` configuration values remain supported and are saved
  as the canonical `video_source` key.
- Completed recordings keep their media authoritative while enrichment runs. A
  matched visible meeting uses the scheduled local time and title for its name;
  hidden or unmatched recordings retain the fallback name. Later Calendar
  refreshes do not retroactively rename or rewrite earlier recordings.
- Speakr publication now follows the configured disabled/manual/automatic policy:
  disabled makes no automatic jobs or daemon attempts, manual retries existing
  explicit jobs, and automatic also enqueues newly completed daemon-detected
  Recordings and `meeting-recorder record` outputs. Existing jobs survive mode
  changes, and no historical directory scan creates jobs.
- Normal Speakr `PATH`, `--all`, and `--retry` attempts require an exact allowed
  active SSID; `--force` bypasses only that SSID gate. Status, relink, and forget
  remain local operations. Unknown, unavailable, or nonmatching Wi-Fi waits, and
  an SSID match does not authenticate Speakr.
- Speakr publication work stays off the GLib loop and reports only action-required
  states. Media transfers remain non-idempotent: uncertain transfers are not
  automatically resent, rejected transfers require an explicit rerun,
  metadata-pending jobs retry only PATCH, and published reruns send nothing.
- Speakr publication now selects an explicit timezone-aware `meeting_date` from
  the visible or hidden Meeting's scheduled start, a valid unmatched sidecar's
  capture start, or file mtime as the final fallback.

## [0.3.5] — 2026-07-19

### Fixed
- **The tray icon was replaced by the floating pill in the top-right corner.**
  0.3.4 removed an argument from `RecordingTray` but left the controller still
  passing it, so building the tray raised `TypeError` every time and the
  controller quietly fell back to the pill. The fallback logged at debug level,
  so a completely broken tray degraded in silence; it now logs a warning with a
  traceback, and a headless test checks that the controller only passes
  arguments the tray actually accepts.
- **The region overlay still painted the screen solid black.** 0.3.3 fixed the
  cairo operators, but the draw handler returned `False`, which in GTK3 lets the
  default handler paint the window's opaque background straight over it. It now
  returns `True` and the window carries a transparent CSS background. Selecting
  an area shows a dimmed screen with the selection punched through it, outlined
  in the theme's colour with a live pixel readout.

### Changed
- GNOME 46 refuses `org.gnome.Shell.Screenshot.SelectArea` to anything but its
  own screenshot tool, so the built-in overlay — not the desktop's picker — is
  what runs on GNOME. The call is kept for desktops that do allow it.

## [0.3.4] — 2026-07-19

### Fixed
- **"Open Folder" did nothing after a manual recording.** The problem was
  lifetime, not wiring: a notification's buttons are callbacks into the process
  that posted them, and `record` printed the saved path and exited immediately,
  leaving a button with nothing behind it. Recordings started by the detector
  were unaffected, because the daemon keeps running — which is why this looked
  inconsistent. `record` now stays alive until the notification is dismissed or
  acted on, capped at three minutes; the file is already written by then.
  A Ctrl-C during that wait exits at once.

### Changed
- The tray menu drops "Open recordings folder" and "Settings…". It is for
  controlling the recording in front of you, not a launcher; what remains is
  the live timer, Pause/Resume and Stop & Save.

## [0.3.3] — 2026-07-19

### Fixed
- **Selected-area recordings came out as diagonal colour smears.** GStreamer
  pads I420 rows to a 4-byte stride while ffmpeg's rawvideo demuxer assumes
  rows are tightly packed, so any width that is not a multiple of 8 offset
  every row and sheared the picture. At width 645 GStreamer wrote 349920 bytes
  where ffmpeg read 348300. The frame size is now decided in one place and
  rounded to a stride-safe width. This was not limited to region capture — a
  1366-wide screen would have corrupted full-screen recordings the same way,
  and 1920 only ever worked by luck.
- A capture region running past the screen edge is now clamped. Unclamped it
  asked x11grab to grab out of bounds, and on Wayland videocrop returned a
  smaller rectangle than the caps demanded, so videoscale upscaled it into a
  blurry stretch of the wrong area.
- **"Open Folder" did nothing when the recordings folder was already open.**
  `nautilus --select` neither re-selects the file nor raises the window in that
  case, and a Wayland client cannot raise itself without an activation token.
  It now uses the freedesktop `FileManager1` interface, so the new recording is
  selected in the existing window.
- Restored the "Open Folder" button on the "saved" notification. Removing it in
  0.3.2 went too far: it is a button, so the file manager only ever appeared on
  a click.
- **The region selector painted the screen solid black** instead of dimming it,
  so there was nothing to aim at. Two cairo mistakes: the dim layer was
  composited with OVER onto an opaque backing (it needs SOURCE, which replaces
  the alpha too), and the "punch the selection out" step passed operator 1 —
  SOURCE — where it meant CLEAR, which is 0.
- Region selection now uses the desktop's own picker
  (`org.gnome.Shell.Screenshot.SelectArea`) where available, so it matches the
  system theme instead of imitating it. Our overlay remains the fallback and
  now draws its marquee in the theme's selection colour.

### Changed
- **X11 is now the recommended session.** Both are supported and tested, but
  Wayland capture depends on `xdg-desktop-portal`: it needs a permission grant
  and, on GNOME 46, can crash mid-session, after which recording continues with
  audio only until it restarts. X11 talks to the display directly.

## [0.3.2] — 2026-07-19

### Added
- **Show or hide the mouse cursor** in the recording. The two backends handle
  this differently: on X11 it is ffmpeg's `-draw_mouse`, while on Wayland the
  compositor composites the pointer, so it is the portal's cursor mode and has
  to be chosen before the stream starts.
- **Drag-select the capture region with the mouse.** This used to shell out to
  `slop`, which is X11-only, so on Wayland the button was permanently disabled
  and the region had to be typed as `x,y,w,h`. It now draws its own full-screen
  overlay — dimmed background, the selection punched out of it, a live pixel
  readout, Escape to cancel — so it works on both session types. `slop` is no
  longer suggested by the package. The result is offset by the monitor origin,
  so regions are correct on a multi-monitor desktop.

### Changed
- Saving in the settings window no longer closes it: changing a setting and
  then pressing "Record now" is a normal thing to want. Save still applies
  immediately and reports it in place.
- The "Recording saved" notification no longer offers to open the folder, so
  finishing a call does not put a file manager in front of what you are doing.
  The tray menu's "Open recordings folder" is unchanged.

## [0.3.1] — 2026-07-19

### Changed
- **Settings that 0.3.0 trimmed too aggressively are back**: file format
  (mkv/mp4), microphone and system-audio volume, and the capture area
  (full screen / current window / selected area) with its `x,y,w,h` region.
  Drag-select still needs `slop`, which is X11-only, so on Wayland the button is
  disabled with a tooltip and the region is typed instead.
- **`stop_debounce_seconds` now defaults to 3s** rather than the 60s introduced
  in 0.3.0, and the spinner steps by 1s. 60s left the recorder visibly running
  for a minute after every call. The consequence is unchanged: because muting
  releases the microphone, this delay is also the longest mute that will not end
  a recording, so raise it if you mute for long stretches.

### Fixed
- The settings window's action buttons sat in two stacked rows, so "Record now"
  and "Reset to defaults" did not share a baseline with "Cancel" and "Save".
  They are now one row.
- The APT repository stopped updating after v0.2.1. It publishes on
  `release: published`, but GitHub suppresses workflow-triggering events for
  anything created with the default `GITHUB_TOKEN` — so once releases were
  published by CI rather than by hand, `apt install meeting-recorder` silently
  kept serving 0.2.1 while newer releases sat on the releases page. The release
  workflow now calls the APT publish directly.

## [0.3.0] — 2026-07-19

### Added
- **Record now** button in the settings window: starts a recording immediately
  without waiting for a meeting to be detected. It runs the `record` subcommand,
  so it reuses the tray icon, timer, pause/resume and the Wayland portal
  handshake rather than reimplementing capture.
- **Reset to defaults** button, with a confirmation. Nothing is written until
  Save, so it can still be backed out by closing the window.

### Changed
- **The settings window is now minimal.** It shows only what changes the result:
  save folder, what to record (screen / microphone / system audio), noise
  cancellation, auto-record, and how long to keep recording after a call ends.
  Nothing was removed from the app — the daemon still reads every key, so file
  format, frame rate, capture mode/region, the volume sliders, normalization and
  the popup timeout remain editable in `~/.config/meeting-recorder/config.json`.
  Capture region in particular was a fixed rectangle that did not follow the
  window, and its drag-select needs `slop`, which is X11-only; on Wayland the
  portal already asks which screen or window to share.
- **"Save" and "Save & Apply" are now one "Save".** The daemon reads its config
  once at startup, so saving without restarting left the new settings silently
  inert. Save now writes, restarts the service and closes the window — staying
  open only if the restart fails.
- **`stop_debounce_seconds` now defaults to 3s** (was 5s) and the settings cap
  rose from 15s to 300s. Because muting releases the microphone, this delay is
  also the longest mute that will not end the recording — raise it if you mute
  for long stretches. The wait is trimmed off the saved file, so a larger value
  costs nothing but a recorder that keeps running after the call.

## [0.2.2] — 2026-07-19

### Fixed
- **The tray icon was white instead of red.** 0.2.1 made it visible by switching
  to `media-record-symbolic`, but gnome-shell recolours every `*-symbolic` icon
  to the panel foreground, so the indicator no longer read as "recording". It
  now ships two non-symbolic panel icons — a red dot while recording, amber
  pause bars while paused — keeping the symbolic names as a fallback for source
  checkouts. (The app icon can't be reused here: its thin dark screen outline
  disappears against a dark panel at 22px.)
- The release workflow hardcoded `Smart Meeting Recorder <tag>` as the release
  title and runs after the tag push, so it renamed releases after the fact. The
  title is now the bare tag.

### Changed
- Release notes no longer repeat the requirements block. It duplicated the
  README on every release and drifted out of date — it still claimed Wayland was
  unsupported on the release that added Wayland support. Requirements now live
  only in the README.

## [0.2.1] — 2026-07-19

### Fixed
- **The tray icon never appeared in the GNOME panel.** The indicator asked for
  `media-record`, which the Yaru theme does not ship — it has only
  `media-record-symbolic`. GTK still resolved the name through the legacy
  Humanity theme, so it looked correct to every local check, but the panel is
  drawn by gnome-shell, which resolves against its own theme, found nothing and
  drew an invisible icon. It now prefers the symbolic names (present in Yaru and
  Adwaita) and falls back to the bundled `meeting-recorder` icon in hicolor.
- The release footer, issue templates and apt landing page still said Wayland
  was unsupported and told users to log in with "Ubuntu on Xorg". The footer is
  appended to every GitHub release, so the 0.2.0 notes contradicted the release
  they were describing.

## [0.2.0] — 2026-07-19

### Added
- **Wayland screen capture.** Sessions are detected automatically: X11 keeps using
  `x11grab`, Wayland captures through the `xdg-desktop-portal` ScreenCast API.
  Because ffmpeg has no PipeWire input device, a small `gst-launch-1.0
  pipewiresrc` pump feeds raw frames into the *same* ffmpeg process that records
  the audio — so there is one process, one clock, and the two-stage
  capture/finalize design (and exact pause/resume) is unchanged.
- The portal permission is requested once and stored as `wayland_restore_token`,
  so later recordings do not re-prompt and Record stays one click.
- `MEETING_RECORDER_CAPTURE=portal|x11` forces a capture backend. The portal path
  also works on GNOME/X11, which makes it testable without changing sessions.

### Fixed
- **Background noise is no longer amplified.** `loudnorm` ran after denoising,
  so it applied whatever gain reached -23 LUFS and lifted room tone up with the
  voice — measured on a silent room, the mic chain came out *9 dB louder* than
  the raw input, leaving the background about as loud as the speaker. A noise
  gate now runs before `loudnorm` (and `afftdn` is slightly stronger): on the
  same mic, room tone drops 26 dB while speech changes 1.5 dB and keeps
  identical peaks.
- `meeting-recorder record` now shows the tray icon, live timer and
  pause/resume controls. It built a bare `Recorder` instead of going through the
  Controller, so a manual recording had no controls at all — on X11 as well as
  Wayland. It now drives the same Controller the daemon uses, and stopping from
  the tray ends the command just like Ctrl-C.
- The tray icon now appears the moment Record is pressed, instead of only after
  capture starts. On Wayland the portal handshake runs first, so the controls
  were missing for the whole handshake — and never appeared at all if the portal
  stalled or crashed, making Record look like it had done nothing.
- A manual `record` is no longer discarded for being shorter than
  `min_recording_seconds`; that rule exists to drop false-positive meeting
  detections, and a manual recording was asked for explicitly.
- The ScreenCast handshake now gives up after 120s instead of hanging forever
  when the dialog is never answered or the portal stops responding.

### Changed
- **Far fewer notifications.** Only two remain: the Record/Ignore prompt, and the
  clickable "saved" result. The "Recording started", "Processing recording…",
  "Recording discarded", "Recording stopped", "Recording audio only",
  "Preparing screen capture…" and service-startup popups are gone — they
  interrupted the call they were reporting on. Live status is the tray icon's
  job; the removed messages are still in the log.
- Denying the screen-share prompt now records audio only instead of failing.
- `.deb` dependencies gained `xdg-desktop-portal` and the GStreamer PipeWire
  plugins; `x11-utils`/`x11-xserver-utils` moved to `Recommends`, as they are
  only needed for the X11 capture path.

### Known limitations
- On Wayland the compositor positions the floating control pill: clients cannot
  place their own windows, and Mutter has no layer-shell protocol.
- Drag-to-select a capture region still needs X11 (`slop`).

## [0.1.2] — 2026-07-17

### Fixed
- `meeting-recorder record` now produces a file. It treated the boolean from
  `stop()` as the saved path and exited immediately, so the background finalize
  pass (concat + denoise + normalize) was killed by the same Ctrl-C that stopped
  capture — leaving orphaned segment files and no output. It now waits for
  finalize to finish and reports the real saved path.

### Internal
- CI builds the `debian/` source package (adds `build-essential`), keeping the
  Launchpad packaging path from drifting from `packaging/build-deb.sh`.

## [0.1.1] — 2026-07-17

### Added
- `meeting-recorder start`, `stop`, `restart` and `logs` — wrappers around
  `systemctl --user` / `journalctl --user`, so the `--user` flag is never needed
  for day-to-day use.

### Changed
- `meeting-recorder status` now reports the background service state (active,
  inactive, failed, or not-installed) alongside the active capture streams and
  the meeting match.
- README and man page explain why this must be a *user* service rather than a
  system one: it needs the caller's X display for screen capture, PulseAudio
  session for audio, and D-Bus session for notifications and the tray icon —
  the same reason `pipewire` is a user service.

## [0.1.0] — 2026-07-17

First release.

### Added
- **Automatic meeting detection** — watches PipeWire/PulseAudio capture streams and
  triggers when a known app (Zoom, Google Meet, Teams, Discord, Slack, Webex, or a
  browser) starts using the microphone. Editable allowlist; debounced to avoid
  false triggers.
- **Permission popup** — "Meeting detected. Start recording?" with Record / Ignore.
  Never records without consent (unless `auto_record` is enabled).
- **Recording** of screen + microphone + optional system audio to `.mkv`, named
  `<App>_<YYYY-MM-DD_HH-MM-SS>`.
- **Automatic stop** when the call ends, with the post-call tail trimmed off so the
  file ends when the call did.
- **Tray controls** — a system-tray icon with a live timer, Pause / Resume,
  Stop & Save, Open recordings folder and Settings. Falls back to a floating
  on-screen pill if the tray is unavailable.
- **Pause excludes paused time** — pausing ends a recording segment and resuming
  starts a new one, so a 20-minute call paused for 5 minutes saves as 15 minutes.
- **Balanced audio** — mic and system audio are each normalized to the same
  loudness target (EBU R128) at finalize, so your voice and the caller's match.
- **Noise cancellation** on the microphone (`afftdn`, or RNNoise via `arnndn` with
  an optional model).
- **Capture area** — full screen, current window, or a selected region.
- **Settings GUI** (`meeting-recorder settings`) for save folder, format, frame
  rate, volumes, normalization, noise cancellation and behavior.
- **Background service** via systemd, idling at a few MB of RAM. `meeting-recorder
  start|stop|restart|logs|status` wrap `systemctl --user`, so the `--user` flag is
  never needed (it must be a *user* service: it needs your X display, audio and
  D-Bus session).
- Debian/Ubuntu `.deb` package.

### Notes / known limitations
- **X11 only.** Screen capture uses `x11grab`; Wayland sessions are not yet
  supported. Log in with "Ubuntu on Xorg" to use this release.
- Window/region capture records a fixed rectangle — moving the window mid-call does
  not move the capture.
- Audio is processed when the recording is saved, so changes to volume settings
  apply to new recordings only.
- Drag-selecting a capture region needs the optional `slop` package.

[0.4.1]: https://github.com/ShadyF/meeting-recorder/releases/tag/v0.4.1
[0.4.0]: https://github.com/ShadyF/meeting-recorder/releases/tag/v0.4.0
[0.3.5]: https://github.com/ssKazal/meeting-recorder/releases/tag/v0.3.5
[0.3.4]: https://github.com/ssKazal/meeting-recorder/releases/tag/v0.3.4
[0.3.3]: https://github.com/ssKazal/meeting-recorder/releases/tag/v0.3.3
[0.3.2]: https://github.com/ssKazal/meeting-recorder/releases/tag/v0.3.2
[0.3.1]: https://github.com/ssKazal/meeting-recorder/releases/tag/v0.3.1
[0.3.0]: https://github.com/ssKazal/meeting-recorder/releases/tag/v0.3.0
[0.2.2]: https://github.com/ssKazal/meeting-recorder/releases/tag/v0.2.2
[0.2.1]: https://github.com/ssKazal/meeting-recorder/releases/tag/v0.2.1
[0.2.0]: https://github.com/ssKazal/meeting-recorder/releases/tag/v0.2.0
[0.1.2]: https://github.com/ssKazal/meeting-recorder/releases/tag/v0.1.2
[0.1.1]: https://github.com/ssKazal/meeting-recorder/releases/tag/v0.1.1
[0.1.0]: https://github.com/ssKazal/meeting-recorder/releases/tag/v0.1.0
