# Smart Meeting Recorder

**Detects when you join an online meeting on Linux and asks whether to capture video, audio only, or
nothing. Stops automatically when the call ends.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Platform: Linux](https://img.shields.io/badge/platform-Linux%20%7C%20X11%20%7C%20Wayland-blue)
![Version](https://img.shields.io/badge/version-0.4.3-green)

No more remembering to hit record. The app sits in the background, notices when a call starts, and
asks:

> **Meeting detected.** Choose what to capture.  ▸ `Video`  `Audio only`  `Ignore`

Choose **Video** for the screen and audio, or **Audio only** when you do not need the screen. Closing
the prompt or letting it time out starts nothing. When a recording ends, the file is saved as
`Zoom_2026-07-17_14-30-05.mkv`.

**Privacy-first:** it never records without your explicit consent unless you enable auto-record,
which starts audio-only immediately without showing the prompt.

| Tray controls | Settings |
|---|---|
| ![Tray icon](docs/images/tray.png) | ![Settings window](docs/images/settings.png) |

---

## ⚠️ Requirements — read this first

| | |
|---|---|
| **Session** | X11 or Wayland — **X11 recommended.** X11 captures directly with `x11grab`. Wayland has to go through `xdg-desktop-portal`, which asks permission the first time and, on GNOME 46, sometimes crashes mid-session; screen capture then falls back to audio-only until it restarts. Both are supported and tested; X11 is simply the calmer path today. |
| OS | Ubuntu 24.04 for source use (built and tested there) |
| Desktop | GNOME (tray icon needs the AppIndicator extension, shipped by default on Ubuntu) |
| Audio | PipeWire or PulseAudio |

Both session types work, and the right capture backend is picked automatically:

```bash
echo $XDG_SESSION_TYPE     # x11 or wayland
```

On **Wayland** the compositor — not the app — owns the screen, so the first recording asks you to
approve screen sharing through your desktop's own dialog. That choice is remembered, so later
recordings stay one click.

**If you have the choice, pick X11.** Wayland works and is tested, but capture depends on
`xdg-desktop-portal`: it needs a permission grant, and on GNOME 46 the portal can crash
mid-session — recording then continues with audio only until it restarts. X11 talks to the display
directly, with none of that in the way. Log in with "Ubuntu on Xorg" from the gear menu on the
login screen. See [Known limitations](#known-limitations) for the rest of the Wayland caveats.

## Run from source

Source use is the supported desktop distribution. Clone the repository and
install the Ubuntu host dependencies:

```bash
git clone https://github.com/ssKazal/meeting-recorder.git
cd meeting-recorder

sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-notify-0.7 \
                 gir1.2-appindicator3-0.1 ffmpeg pulseaudio-utils \
                 xdg-desktop-portal gstreamer1.0-pipewire gstreamer1.0-tools \
                 gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
                 x11-utils x11-xserver-utils

```

No Python packages are required: the application runs on the system `python3`
with the distribution's PyGObject.

### Activate desktop integration

From the checkout root, install the user-local launcher, app-grid entries,
icons, and user service. This does not start the detector:

```bash
./scripts/install-source.sh
```

Start it explicitly after installation, or ask the installer to enable and
start it in one step:

```bash
systemctl --user enable --now meeting-recorder.service
# or: ./scripts/install-source.sh --enable
```

The launcher is `~/.local/bin/meeting-recorder`, so ensure `~/.local/bin` is
on `PATH` before using the command examples below. The installer only replaces
the launcher and XDG links it created; it stops safely on caller-owned file
conflicts.

To update an existing activation, update the checkout and rerun the installer;
then restart the service if it is running:

```bash
git pull --ff-only
./scripts/install-source.sh
systemctl --user restart meeting-recorder.service
```

For foreground use without desktop activation, run directly from the checkout:

```bash
python3 -m meeting_recorder run
```

To remove source integration, run this from the activated checkout. It stops
and disables the user service, removes only installer-owned launcher and XDG
links, and leaves the checkout, recordings, and configuration untouched:

```bash
./scripts/install-source.sh --remove
```

Protected release tags also start runtime-image publication to GHCR under the
policy in [Container images](docs/CONTAINER-IMAGES.md). Do not infer that an
image is publicly available until its release verification succeeds.

## Runtime image

The repository-root `Containerfile` defines a separate runtime-image path for
Ubuntu 24.04 on `linux/amd64`. It copies the application source to
`/opt/meeting-recorder` and directly executes `python3 -m meeting_recorder ...`.
The image includes the client-side GTK/PyGObject,
ffmpeg/GStreamer, xdg-desktop-portal, PulseAudio, D-Bus, and Secret Service
tooling needed by the application. It runs the application from source and does
not use `pip`.

This is not the headless development container. The `.devcontainer` image is
kept for source checks and ordinary development; it deliberately does not
provide desktop capture or host integration.

Use the repository smoke path from the checkout root:

```bash
./scripts/test-runtime-image.sh
```

For a manual local Buildx build, use the same file and architecture explicitly:

```bash
docker buildx build -f Containerfile --platform linux/amd64 --load \
  -t meeting-recorder:runtime .
```

The image defaults are `XDG_CONFIG_HOME=/config`, `XDG_STATE_HOME=/state`,
`XDG_CACHE_HOME=/cache`, and `/opt/meeting-recorder` as the source/work
directory. Its launcher is the image entrypoint; with no explicit command, it
invokes `python3 -m meeting_recorder run`. The launcher is
`/usr/local/bin/meeting-recorder`; the image does not select a fixed user.
Writable XDG directories and the configured Recording directory, including
their ownership and persistence, are runtime responsibilities rather than image
defaults.

Before starting the daemon, the runtime must provide a valid `TZ`, the
Wayland display socket, the PulseAudio socket, and the session bus. Headless
administrative commands can run without starting the daemon. A missing system
bus, Secret Service, Speakr token, or Calendar OAuth configuration disables only
the related optional behavior;
it does not turn that missing integration into a capture requirement.

The image contains clients, not the desktop services. The compositor, portal
backend, PipeWire/Pulse server, session and system buses, NetworkManager,
Secret Service keyring, and browser remain host-side services. Wayland capture
uses the portal contract; the image does not assume or require a raw PipeWire
socket mount.

This issue defines an image and local-smoke contract, not a deployment recipe or
a production-readiness claim. Issue #27 defines the intended GHCR release
behavior; see [Container images](docs/CONTAINER-IMAGES.md). Do not infer that a
public image already exists until its first publish and anonymous digest smoke
succeed. Quadlet confinement and lifecycle belong to #28, and real Bluefin host
validation belongs to #29. Live capture has not been validated by this image
contract.

### Bluefin rootless Podman operators

For the canonical direct-command Quadlet guide—including the pinned digest,
trusted-desktop security boundary, exact desktop sockets, manual lifecycle,
update, rollback, secret-file handling, and removal steps—see [Bluefin GNOME /
rootless Podman operator guide](docs/BLUEFIN.md). It is not evidence of
real-host graphical validation; issue #29 owns that validation.

<details>
<summary>Optional: better noise cancellation</summary>

```bash
# Studio-grade mic denoising (RNNoise). Then set
# "noise_model_path": "~/.local/share/meeting-recorder/std.rnnn" in your config.
mkdir -p ~/.local/share/meeting-recorder
curl -L -o ~/.local/share/meeting-recorder/std.rnnn \
  https://raw.githubusercontent.com/GregorR/rnnoise-models/master/somnolent-hogwash-2018-09-01/sh.rnnn
```
</details>

## Usage

After source activation, once the detector is running, join a call and choose **Video**, **Audio only**, or
**Ignore** in the popup. Closing it or leaving it unanswered starts no recording. Recordings land
in `~/Videos/MeetingRecorder/`.

### Tray controls

While recording, an icon appears in the top bar. Click it for:

- **● Recording — 01:23** — live timer
- **Pause / Resume**
- **Stop & Save**
- **Open recordings folder**
- **Settings…**

**Pause excludes paused time.** Record for 20 minutes but pause from 10:00–15:00 and the saved file
is **15 minutes** — the paused span never reaches the file.

There's no "recording started" notification; the tray icon already shows it. When you stop, audio is
balanced in the background — you'll see a brief *"Processing recording…"*, then **"Recording saved"**
with a **📁 Open Folder** button.

If requested screen capture is unavailable, a visible notification confirms that recording continued
audio-only.

### Settings

After source activation, open **Meeting Recorder Settings** from your app grid, or:

```bash
meeting-recorder settings
```

Change save folder, format, frame rate, mic/system volume, normalization, noise cancellation,
capture area and behavior. **Save & Apply** restarts the service so changes take effect.

### Command line

After source activation, use the user-local launcher:

```bash
meeting-recorder status     # service state + capture streams + meeting match
meeting-recorder start      # start the background service
meeting-recorder stop       # stop it (pause detection)
meeting-recorder restart    # restart it (apply setting changes)
meeting-recorder logs       # follow the service log
meeting-recorder settings   # settings window
meeting-recorder run        # run the detector in the foreground
meeting-recorder record     # record right now until Ctrl-C
meeting-recorder config     # create/print the config file
meeting-recorder calendar connect|status|disconnect  # manage Calendar credentials
meeting-recorder calendar client-secret set|status|clear  # manage a required client secret
meeting-recorder speakr upload PATH [--force]         # publish one Recording
meeting-recorder speakr upload --all [--force]       # attempt all due jobs
meeting-recorder speakr upload --status JOB
meeting-recorder speakr upload --status --all
meeting-recorder speakr upload --retry JOB [--force]
meeting-recorder speakr upload --relink JOB NEW_PATH
meeting-recorder speakr upload --forget JOB
meeting-recorder cleanup --older-than DAYS [--delete]
```

`start`/`stop`/`restart`/`logs` wrap `systemctl --user`, so you never need to
remember the `--user` flag. The equivalents are:

```bash
systemctl --user status|start|stop|restart meeting-recorder
journalctl --user -u meeting-recorder -f
```

> **Why `--user`?** This is a *user* service, not a system one: it needs your X
> display (screen capture), your PulseAudio session (mic/system audio) and your
> D-Bus session (notifications, tray icon). A system service runs as root with
> none of those, so it must run in your session — hence `--user`, or just use the
> wrapper commands above.

### Optional Google Calendar connection

Calendar support uses private, bounded offline snapshots to enrich completed
recordings without delaying capture. Create a **Desktop** OAuth client that you own in Google
Cloud, enable the Google Calendar API, and put only its bare public client ID (ending in
`.apps.googleusercontent.com`) in `google_calendar_client_id`. Google's Desktop client
documentation describes a client secret as optional, but some real Desktop client
configurations require one at the token endpoint. If yours does, manage it with the
`client-secret` commands below. Never put the secret, downloaded credential JSON,
authorization code, or token in the config.

```bash
meeting-recorder calendar connect
meeting-recorder calendar status
meeting-recorder calendar disconnect
meeting-recorder calendar client-secret set
meeting-recorder calendar client-secret status
meeting-recorder calendar client-secret clear
meeting-recorder calendar list
meeting-recorder calendar select --id "calendar-id"
meeting-recorder calendar refresh
meeting-recorder calendar correct path/to/recording.mkv
```

`connect` starts a short-lived listener on `127.0.0.1`, opens your browser, and requests only
Calendar-list and event read-only access. The refresh token is stored in your desktop's Secret
Service, not in the config file, environment, recordings, or sidecars. When a client secret is
required, `calendar client-secret set` reads it through a hidden interactive prompt and stores it
only in Secret Service, associated with the configured public client ID. This is the only command
that requires a real interactive TTY; do not pass the secret as an argument, pipe it, or place it
in a shell variable. The secret is sent only to Google's token endpoint, never to the browser
authorization URL, Calendar API requests, config, environment, credential JSON, or Quadlet.
`status` and `clear` can run without a TTY.
For valid Secret Service storage, `calendar client-secret status` reports one of
`absent`, `configured`, or `client-ID mismatch` without printing the secret.
Malformed or unavailable Secret Service storage fails safely without exposing
stored contents. `calendar client-secret clear` explicitly removes the secret
and remains usable even when the public client ID is absent or malformed.

`disconnect` tries to revoke the refresh token and always attempts to remove the local refresh
token and Calendar-only cache. It retains the public client ID, loopback configuration, and any
stored client secret. Use `calendar client-secret clear` when the client secret itself must be
removed. It reports a nonzero cleanup failure if either local removal cannot be confirmed. During
a temporary Google or network outage, `calendar status` reports `connected` with `credential
present; validation unavailable` and a nonzero exit status; it retains the credential rather than
treating it as expired.

The listener normally uses an OS-selected port. Set `google_calendar_loopback_port` to a fixed
integer from 1 through 65535 only when a container, sandbox, or firewall requires it; that exact
port must be reachable by the browser and available before consent opens. A running, unlocked
Secret Service session is required for connect/status/disconnect; containers without a session
D-Bus or secret backend report Calendar as misconfigured while recording continues normally.

Select calendars explicitly with `calendar select`; Google Calendar's own selected flag is never
used. Selected event snapshots are private, bounded to a recent offline window, and accepted for
at most seven days. Refresh runs in the background every 15 minutes and can also be requested from
the CLI. Calendar failures never delay or change recording.

### Recording meeting metadata

When a recording finishes, the daemon performs a synchronous, cache-only Calendar
match. A matched recording is renamed to
`YYYY-MM-DD_HH-MM-SS_Title.mkv` using the event's local scheduled time; collisions
receive `-2`, `-3`, and so on. Hidden meetings and unmatched recordings retain
their fallback filename. Every recording has an adjacent
`<media filename>.meeting.json` sidecar (schema version 1) containing the capture
interval, fallback name, stable occurrence selector, and the current visible
meeting snapshot. Sidecars are written atomically and are not required for the
media file to remain usable.

Use the correction command to inspect or change one recording from the fresh
offline cache:

```bash
meeting-recorder calendar correct path/to/recording.mkv
meeting-recorder calendar correct path/to/recording.mkv --refresh
meeting-recorder calendar correct path/to/recording.mkv --select SELECTOR
meeting-recorder calendar correct path/to/recording.mkv --clear
```

The default command lists nearby cached occurrences. `--refresh` explicitly
performs a blocking Calendar refresh first, then falls back to usable cached data
if refresh fails. `--select` changes the match and visible filename; `--clear`
restores the collision-safe fallback name and removes the sidecar. Correction
never stores credentials, prints private meeting details, or mutates old files
because a later Calendar refresh changes its cache. Metadata, rename, and sidecar
failures leave the authoritative media in place.

### Optional Speakr publication

Speakr publication is optional and policy-controlled. Set
`speakr_publication_mode` to one of the following values; the shipped default is
`disabled`:

| Mode | Newly completed Recordings | Existing explicit jobs |
|---|---|---|
| `disabled` | Not enqueued automatically | No daemon attempts |
| `manual` | Not enqueued automatically | The daemon retries due jobs |
| `automatic` | The daemon and `meeting-recorder record` enqueue completed Recordings | The daemon retries due jobs |

Explicit `meeting-recorder speakr upload ...` commands remain available in every
mode. Changing the mode preserves existing Publication jobs, and the daemon never
scans historical directories to create jobs. For `meeting-recorder record`, the
automatic enqueue happens after its GLib loop exits; the daemon worker attempts the
durable job later.

Configure the production Speakr origin with `speakr_url`; it must use HTTPS. For
native/source use, provide the bearer token only through
`MEETING_RECORDER_SPEAKR_TOKEN`:

```bash
MEETING_RECORDER_SPEAKR_TOKEN='…' meeting-recorder speakr upload PATH
```

The token is never accepted from the config file or CLI arguments and is never
stored in SQLite. Durable publication progress fields are public-only. The
database also stores one protected private filesystem locator for the current
Recording path as operational state inside the protected 0700 state directory
and 0600 SQLite boundary. The database contains no credentials or copied private
Meeting title, notes, or participants. Publication state lives at
`$XDG_STATE_HOME/meeting-recorder/publications.sqlite3` (or
`~/.local/state/meeting-recorder/publications.sqlite3` when `XDG_STATE_HOME` is
unset). The Bluefin Quadlet instead mounts the token as its fixed mode-`0400`
secret file; see [Bluefin GNOME / rootless Podman operator guide](docs/BLUEFIN.md).

Automatic and normal explicit network attempts are admitted only when
NetworkManager reports an active Wi-Fi SSID that exactly matches an entry in
`speakr_allowed_ssids`. Entries are case-sensitive strings compared as their raw
UTF-8 bytes: they must be non-empty and at most 32 UTF-8 bytes, with no trimming,
case folding, or Unicode normalization. For example:

```json
{
  "speakr_publication_mode": "automatic",
  "speakr_allowed_ssids": ["Office Wi-Fi"]
}
```

Unknown, unavailable, or nonmatching Wi-Fi is not admission; the worker waits for
a later check and does not contact Speakr. An SSID match is only an admission
gate, not authentication. HTTPS, token authentication, recording-hash and file
identity checks, lease fencing, reconciliation rules, and other file-safety checks
still apply.

The normal network forms are `upload PATH`, `upload --all`, and `upload --retry
JOB`; each is SSID-gated. `--force` is accepted only with those three forms and
bypasses only the SSID gate. The local forms `upload --status JOB`, `upload
--status --all`, `upload --relink JOB NEW_PATH`, and `upload --forget JOB` do not
perform network publication and do not use the SSID gate.

The daemon's publication worker keeps hashing, SQLite access, token reads,
NetworkManager D-Bus probes, and Speakr network I/O off the GLib loop. It reports
only action-required publication states, and a publication failure never changes
whether a Recording succeeded. Publication jobs retain the durable recovery
semantics described below: uncertain media transfers are not automatically resent,
`metadata_pending` retries only the metadata PATCH, and a `published` rerun sends
nothing. Use explicit `--retry` when an action-required state requires operator
authorization.

For a matched visible Meeting, the publisher sends the current title, scheduled
time, description/location notes, and participants. A hidden matched Meeting
uses the current filename title and its non-private scheduled start, without
private notes or participants. An unmatched Recording with a valid sidecar uses
the current filename title and capture start, without notes or participants. If
there is no valid sidecar, it uses the current filename title and file mtime.
The metadata POST includes an explicit timezone-aware `meeting_date` and
`file_last_modified`. After the media transfer, the publisher rereads the
current filename and adjacent sidecar before sending the authoritative metadata
PATCH, so a rename or updated visible Meeting metadata is not taken from the
stale upload snapshot.

The media POST is non-idempotent: `transfer_unknown` is never automatically
resent. A rejected transfer may be retried only by explicitly rerunning the
command. `metadata_pending` retries only the metadata PATCH, while a `published`
rerun sends no requests.

### Explicit cleanup of published Recordings

Cleanup is a local, explicit operation for removing old Recordings after Speakr
publication:

```bash
meeting-recorder cleanup --older-than 30
meeting-recorder cleanup --older-than 30 --delete
```

`DAYS` must be an integer of at least `1`. Without `--delete`, the command only
previews its decisions and does not change the publication database or the
recordings directory. `--delete` performs irreversible deletion. It removes the
Recording and, when present, its exact adjacent
`<media filename>.meeting.json` sidecar; unrelated neighboring files are not
part of the cleanup. There is no Trash or application recovery path, so keep a
separate copy if you may need the local files later.

Cleanup considers only a complete same-path publication group whose every job is
`published`, has no active publication or cleanup lease, and records the same
Recording SHA-256. The Recording must be older than the requested cutoff. Age is
taken from a valid adjacent sidecar's capture end time, or from the durable
source mtime when no sidecar exists; a malformed or mismatched sidecar does not
fall back to the mtime. Before deletion, cleanup rechecks the media hash and
exact file identities. Unsafe, malformed, mismatched, symlinked, hardlinked,
out-of-root, or changed media and sidecar entries, or unsafe path components,
are refused and reported incomplete rather than trusted or removed.

Deletion records a durable cleanup intent and advances through hidden quarantine
names and filesystem durability checkpoints. If the process stops after a
mutation, the intent and quarantine state remain for inspection and safe
resumption. Only a later explicit `meeting-recorder cleanup --older-than DAYS
--delete` may resume it; preview, the daemon, publication automation, tokens,
SSID checks, D-Bus, and HTTP never start or resume cleanup. After successful
completion, the publication jobs remain in the database as `local_removed` so
publication history is retained, while the local path is no longer retained.

## Configuration

Recording settings are available in the GUI, but the config file is
`~/.config/meeting-recorder/config.json` (it **overrides** the shipped defaults).
Speakr publication policy is configured in this file rather than through the GUI;
restart the service after changing these keys.

| Key | Meaning |
|-----|---------|
| `output_dir` | Where recordings are saved (default `~/Videos/MeetingRecorder`) |
| `container` | `mkv` (default, crash-safe) or `mp4` |
| `framerate`, `video_codec`, `video_preset` | Encoding options |
| `record_screen` | Default media composition for a manual recording: audio-video when true, audio-only when false |
| `record_mic` / `record_system_audio` | Toggle independent audio sources |
| `video_source` | `fullscreen`, `window` (focused window), or `area` |
| `capture_region` | `"x,y,w,h"` when `video_source` is `area` |
| `normalize_voice` | Normalize **both** your mic and the caller to the same loudness so both voices match (default `true`) |
| `mic_volume` / `system_volume` | Fine-trim after normalization (`1.0` = equal). Raise `mic_volume` to sit above the caller |
| `noise_cancellation` | Filter background noise from the mic (default `true`) |
| `noise_model_path` | Optional RNNoise `.rnnn` model for better denoising |
| `auto_record` | Skip the popup and start an audio-only recording immediately |
| `prompt_timeout_seconds` | How long the popup waits before closing without recording |
| `show_cursor` | Draw the mouse pointer into the recording. On X11 this is ffmpeg's `-draw_mouse`; on Wayland the compositor decides, so it is the portal's cursor mode. |
| `start_debounce_seconds` / `stop_debounce_seconds` | How long audio must be present/absent before starting/stopping. Because muting releases the microphone, the stop delay is also the longest mute that won't end the recording — raise it if you mute for long stretches. The wait is trimmed off the saved file. |
| `poll_interval_seconds` | How often capture streams are checked |
| `min_recording_seconds` | Discard recordings shorter than this |
| `google_calendar_client_id` | Optional bare user-owned Google Desktop OAuth client ID; a required client secret is managed separately with `calendar client-secret`, never in config or credential JSON |
| `google_calendar_loopback_port` | OAuth loopback port: `0` for an OS-selected port (default), or `1`--`65535` |
| `speakr_url` | Public Speakr HTTPS origin in production; native/source use supplies the bearer token through `MEETING_RECORDER_SPEAKR_TOKEN` (the Bluefin Quadlet uses its documented secret file) |
| `speakr_publication_mode` | `disabled` (default), `manual`, or `automatic`; controls automatic enqueueing and daemon attempts |
| `speakr_allowed_ssids` | `[]` by default; exact, case-sensitive Wi-Fi SSID strings, each at most 32 UTF-8 bytes; no trimming or normalization |
| `allowlist` | `{"match": "<substring>", "app": "<Display Name>"}` rules |

To watch another app, add an allowlist entry matching its process name:

```json
{"match": "webex", "app": "Webex"}
```

## How it works

**Detection.** A meeting means *a known app is actively using your microphone*. PipeWire/PulseAudio
exposes every mic capture as a stream tagged with the owning app, so `pactl` gives one reliable
signal that covers Zoom, Teams, Discord, Slack and browser calls alike. Streams that only tap system
audio (music players) are ignored, so background audio never triggers it. A debounce prevents brief
audio drops from starting or stopping a recording.

**Recording is two-stage.** Live capture writes the video plus your mic and the system audio as
*separate, unprocessed* tracks — no filters, so there's no latency and pause can cut exactly. When
you stop, a finalize pass concatenates the segments, denoises the mic, normalizes both audio sources
to the same loudness (EBU R128), mixes and limits them — and **stream-copies the video**, so it's
quick. This is why both voices come out level and why pausing is exact.

## Troubleshooting

**Nothing happens when I join a meeting**
```bash
meeting-recorder status        # is your app listed and matched?
journalctl --user -u meeting-recorder -f
```
If your app isn't matched, add it to the `allowlist`.

**Black screen / no video on Wayland** — screen sharing was probably denied, in which case the
recording keeps the audio and drops the video. Clear the stored permission and you'll be asked
again on the next recording:
```bash
meeting-recorder config     # then set "wayland_restore_token" back to ""
```
Check `gst-inspect-1.0 pipewiresrc` prints a plugin — without `gstreamer1.0-pipewire` installed,
Wayland capture cannot work. To force a backend while debugging:
`MEETING_RECORDER_CAPTURE=portal|x11 meeting-recorder run`.

**No tray icon** — the GNOME AppIndicator extension must be enabled:
```bash
gnome-extensions enable ubuntu-appindicators@ubuntu.com
```
Without it, a floating control pill appears instead.

**My voice is too quiet / the caller is too loud** — keep `normalize_voice: true` and leave both
volumes at `1.0`; that makes them equal. Nudge `mic_volume` to `1.2` to sit slightly above the caller.

**Recording is choppy** — 1080p30 is demanding. Lower `framerate` to `20`, or set
`video_preset` to `ultrafast`.

**Changes to settings did nothing** — restart the service (`systemctl --user restart
meeting-recorder`), and make sure you edited `~/.config/meeting-recorder/config.json`.

## Remove local source data

If the Calendar client secret itself must be removed, run
`meeting-recorder calendar client-secret clear` before removing source
integration or the checkout. Remove source integration with
`./scripts/install-source.sh --remove`, then remove the checkout when it is no
longer needed. To remove local settings as well (this does not remove a
Calendar client secret from Secret Service):

```bash
rm -rf ~/.config/meeting-recorder
```

Your recordings in `~/Videos/MeetingRecorder/` are never touched.

## Source development

```bash
git clone https://github.com/ssKazal/meeting-recorder.git && cd meeting-recorder
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-notify-0.7 \
                 gir1.2-appindicator3-0.1 ffmpeg pulseaudio-utils \
                 xdg-desktop-portal gstreamer1.0-pipewire gstreamer1.0-tools \
                 gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
                 x11-utils x11-xserver-utils

python3 -m meeting_recorder run     # run from source
python3 tests/run_tests.py          # tests — zero dependencies, no pytest needed
```

No Python packages are required — the app runs on the system `python3` with the distro's PyGObject.
See [CLAUDE.md](CLAUDE.md) for the architecture rationale (especially *why audio filters must not run
during live capture*).

## Known limitations

- **X11 is the recommended session.** Wayland is supported and tested, but its capture path goes
  through `xdg-desktop-portal`, which is an extra moving part: it asks permission, and on GNOME 46
  it can crash mid-session, after which recording falls back to audio only until it restarts.
- **Wayland asks permission once** — the portal dialog appears on the first recording; the answer is
  remembered in `wayland_restore_token`. Denying it records audio only.
- **On Wayland the control pill is placed by the compositor** — Wayland clients cannot position their
  own windows, so it may not sit in the top-right corner. The tray icon is unaffected.
- **Window/region capture is a fixed rectangle** — moving the window mid-call won't move the capture.
- **Audio is processed at save time**, so volume/normalization changes apply to new recordings only.
  A 1-hour meeting takes a few minutes to finalize in the background.
- Browser calls are detected by mic use, so any browser mic usage counts as a call.

## Contributing

Issues and pull requests are welcome. Please run `python3 tests/run_tests.py` before submitting.

## License

[MIT](LICENSE) — see [CHANGELOG.md](CHANGELOG.md) for release notes.
