# Meeting Recorder on Bluefin GNOME

This guide runs Meeting Recorder as a rootless Podman **user** Quadlet in a
local Bluefin GNOME session. It uses the published `0.4.3` image and does not
need a repository clone. Run the commands as the logged-in desktop user, in
**Fish**; do not use `sudo` or a remote/SSH session.

> **Trust warning:** this is a trusted-desktop container, not a strong sandbox.
> It receives sensitive access to your Wayland display, Pulse audio, and session
> D-Bus. The supplied Quadlet also sets `SecurityLabelDisable=true`, disabling
> per-container SELinux process-label separation. Download and run it only if
> you trust the image and its configuration.

## 1. Prepare private data directories and recording settings

Create the dedicated host directories. `MR_RECORDINGS` may be another location,
but it must be an absolute path. It must match both `output_dir` and the
Recording `Volume=` in the Quadlet exactly.

```fish
set MR_CONFIG "$HOME/.config/meeting-recorder"
set MR_STATE "$HOME/.local/state/meeting-recorder"
set MR_CACHE "$HOME/.cache/meeting-recorder"
set MR_RECORDINGS "$HOME/Videos/MeetingRecorder"

install -d -m 700 "$MR_CONFIG" "$MR_STATE" "$MR_CACHE" "$MR_RECORDINGS"
realpath "$MR_RECORDINGS"
vi "$MR_CONFIG/config.json"
```

In the editor, add a JSON configuration like the following. Replace the sample
`output_dir` with the literal path printed by `realpath` above—do not use `~`,
`$HOME`, or a relative path in JSON.

```json
{
  "output_dir": "/var/home/alex/Videos/MeetingRecorder",
  "record_screen": true,
  "video_source": "fullscreen",
  "record_mic": true,
  "record_system_audio": true,
  "framerate": 30,
  "container": "mkv",
  "google_calendar_loopback_port": 8765,
  "google_calendar_client_id": "",
  "speakr_url": "",
  "speakr_publication_mode": "disabled",
  "speakr_allowed_ssids": []
}
```

Use `record_screen`, `video_source` (`fullscreen`, `window`, or `area`),
`record_mic`, `record_system_audio`, `framerate`, and `container` to set the
initial recording behavior. Once the service is running,
`podman exec meeting-recorder meeting-recorder settings` opens the settings
window; a successful save causes the managed service to restart.
`podman exec meeting-recorder meeting-recorder config` prints or creates the
container-visible configuration.

## 2. Pull the image and install the Wayland Quadlet

The supplied Quadlet is downloaded directly from the matching deployment tag,
then its digest image reference is deliberately changed to the published version
tag requested here. `Pull=never` remains in the unit, so updates are always an
explicit operator action.

```fish
set IMAGE ghcr.io/shadyf/meeting-recorder:0.4.3
set QUADLET_DIR "$HOME/.config/containers/systemd"
set UNIT "$QUADLET_DIR/meeting-recorder.container"

podman pull "$IMAGE"
podman image inspect "$IMAGE"

install -d -m 700 "$QUADLET_DIR"
curl --fail --location \
  --output "$UNIT" \
  https://raw.githubusercontent.com/ShadyF/meeting-recorder/bluefin-v0.4.3/deploy/bluefin/meeting-recorder.container

string replace --regex '^Image=.*$' "Image=$IMAGE" < "$UNIT" > "$UNIT.new"; and mv "$UNIT.new" "$UNIT"
grep '^Image=' "$UNIT"
```

The downloaded unit is the Wayland profile. Confirm the active session and
socket before starting it:

```fish
printf 'session=%s  Wayland display=%s\n' "$XDG_SESSION_TYPE" "$WAYLAND_DISPLAY"
test "$XDG_SESSION_TYPE" = wayland
test -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY"
```

`wayland-0` is the normal display. If the printed display is another value,
edit the downloaded `$UNIT` and change **both** occurrences of
`wayland-0` in the Wayland `Volume=` line and the
`Environment=WAYLAND_DISPLAY=` line to that display name. For example, replace
both with `wayland-1`; leave the other socket mounts unchanged. This guide is
for Wayland; do not use its Wayland socket configuration unchanged in an X11
session.

Before starting, also edit `$UNIT` to set `Environment=TZ=` to your IANA time
zone (for example, `Europe/London`). If `MR_RECORDINGS` is not the default
`$HOME/Videos/MeetingRecorder`, change the Recording `Volume=` so the host and
container sides are the same absolute path as `output_dir`.

```fish
vi "$UNIT"
```

The unit maps the Calendar callback only to `127.0.0.1:8765`. Keep that mapping
and `google_calendar_loopback_port: 8765` together; do not expose it on the
network or silently select another port.

## 3. Optional Speakr token

The downloaded Quadlet has a required `Secret=` line even when Speakr is
disabled. Choose one path **before** the first service start:

- **Use Speakr:** create the rootless Podman secret with a hidden Fish prompt.
  The token is supplied on standard input, never in the configuration, command
  arguments, or shell history.

  ```fish
  read --silent --prompt-str 'Speakr token: ' SPEAKR_TOKEN
  printf '\n'
  printf '%s' "$SPEAKR_TOKEN" | podman secret create meeting-recorder-speakr-token -
  set --erase SPEAKR_TOKEN
  ```

- **Do not use Speakr:** remove only the single `Secret=` line from `$UNIT`
  before starting. Do not use a placeholder token or put a token in an
  environment variable.

For Speakr, set `speakr_url` to its HTTPS origin and choose
`manual` or `automatic` for `speakr_publication_mode`. An empty
`speakr_allowed_ssids` list disables SSID gating. Otherwise, add exact,
case-sensitive Wi-Fi SSIDs; normal upload attempts are admitted only on a
matching active Wi-Fi SSID, or a confirmed active non-Wi-Fi connection.
`disabled` is the default and retains no automatic publication behavior.

When publication mode is `automatic`, an explicitly accepted **Video** or
**Audio only** detected Recording can show a separate non-modal tag checklist
after capture is active. It is not shown for unattended auto-record, manual or
disabled publication modes, or `meeting-recorder record`; it never overlaps the
Wayland portal or blocks Pause/Stop. **Done** freezes zero or more existing tags,
**Skip** freezes none, and close/Escape/Not now/untouched 15-second timeout discard
unsaved edits. During that Recording, use its tag control to reopen the frozen
catalog or manually retry a missing catalog. Discovery and validation each have a
five-second budget. Tag discovery ignores the SSID gate; only media-publication
commands use that network admission policy.

Meeting Recorder targets Speakr `v0.10.5-alpha`: it reads the catalog with
authenticated `GET /api/v1/tags`, sends tags only on the initial upload, and never
changes remote tags after acceptance. A successful validation sends only accessible
IDs; a transient validation failure sends frozen IDs and reports the result as
unknown in CLI status. Its active-origin catalog cache is private under `$MR_CACHE`
and contains no token. The bearer token remains only the rootless Podman secret
file. Use
`podman exec meeting-recorder meeting-recorder speakr upload --status JOB` for
CLI-only `effective_tags`, `missing_tags`, `upload_tags_unknown`, and
`sidecar_warning` fields; tag warnings do not change capture or cause desktop
notifications.

## 4. Start and operate the service

Reload the user Quadlet generator, then explicitly start the generated service.
It is associated with the graphical session; do not enable it as an ordinary
unit.

```fish
systemctl --user daemon-reload
systemctl --user start meeting-recorder.service
systemctl --user status meeting-recorder.service
podman ps --filter name=meeting-recorder
```

Use these common lifecycle and log commands. Stop the service normally and give
an active recording time to finalize; do not use `podman kill`.

```fish
systemctl --user status meeting-recorder.service
systemctl --user start meeting-recorder.service
systemctl --user stop meeting-recorder.service
systemctl --user restart meeting-recorder.service
journalctl --user -u meeting-recorder.service -f
journalctl --user -u meeting-recorder.service --since '10 minutes ago'
```

Run application administration inside the running container. Paths passed to
recording commands use the same absolute recording path configured above. In a
later Fish session, set `MR_RECORDINGS` again to that path before using the path
examples below.

```fish
podman exec meeting-recorder meeting-recorder settings
podman exec meeting-recorder meeting-recorder config

podman exec meeting-recorder meeting-recorder speakr upload "$MR_RECORDINGS/example.mkv"
podman exec meeting-recorder meeting-recorder speakr upload --all
podman exec meeting-recorder meeting-recorder speakr upload --status --all
podman exec meeting-recorder meeting-recorder speakr upload --retry JOB
podman exec meeting-recorder meeting-recorder speakr upload --relink JOB "$MR_RECORDINGS/relinked.mkv"
podman exec meeting-recorder meeting-recorder speakr upload --forget JOB

# This previews candidates and changes no files.
podman exec meeting-recorder meeting-recorder cleanup --older-than 30

# WARNING: This permanently deletes eligible published recordings and sidecars.
podman exec meeting-recorder meeting-recorder cleanup --older-than 30 --delete
```

Normal Speakr network commands are SSID-gated. Add `--force` only to an explicit
`upload PATH`, `upload --all`, or `upload --retry JOB` when intentionally
bypassing that gate; it does not bypass token authentication or other checks.

## Google Calendar

In Google Cloud, enable the Google Calendar API and create a **Desktop** OAuth
client that you control. Put only its bare public client ID in
`google_calendar_client_id`; do not put downloaded credentials, authorization
codes, refresh tokens, or a client secret in `config.json`. This deployment uses
the fixed loopback callback `http://127.0.0.1:8765/oauth2/callback`.

Some Desktop clients require a client secret at the token endpoint. If yours
does, set it through the hidden interactive command below. It is stored through
the desktop Secret Service, not in the config, Quadlet, environment, or command
history. Do not pipe or pass the secret as an argument.

```fish
podman exec meeting-recorder meeting-recorder calendar connect
podman exec meeting-recorder meeting-recorder calendar status
podman exec -it meeting-recorder meeting-recorder calendar client-secret set
podman exec meeting-recorder meeting-recorder calendar client-secret status
podman exec meeting-recorder meeting-recorder calendar list
podman exec meeting-recorder meeting-recorder calendar select --id 'calendar-id'
podman exec meeting-recorder meeting-recorder calendar refresh
podman exec meeting-recorder meeting-recorder calendar disconnect

# Use this only when the separately stored client secret must be removed.
podman exec meeting-recorder meeting-recorder calendar client-secret clear
```

`connect` opens the host browser for consent. Calendar credentials require an
available, unlocked desktop Secret Service. `disconnect` removes the Calendar
refresh token and cache but retains the public client ID and any separately
stored client secret; use `client-secret clear` to remove that secret.

## Update or remove

To update, stop cleanly, pull the desired published version tag, back up the
unit, change only its `Image=` line, then reload and start. Keep the previous
image while it is a rollback option.

> **Publication data compatibility warning:** the first start of a build using
> Publication store v4 automatically and permanently resets an exact v3
> `publications.sqlite3` store with no application backup. Queued jobs, retries,
> remote recording IDs, and cleanup intents/history are lost; local Recordings
> and sidecars remain. This is not an old-database migration. Do not treat the
> previous image as a rollback option after that reset: rollback to the prior
> binary is unsupported. Preserve any required Publication information before
> starting the newer image.

```fish
set NEW_IMAGE ghcr.io/shadyf/meeting-recorder:NEW_VERSION
set UNIT "$HOME/.config/containers/systemd/meeting-recorder.container"

systemctl --user stop meeting-recorder.service
podman pull "$NEW_IMAGE"
cp "$UNIT" "$UNIT.previous"
string replace --regex '^Image=.*$' "Image=$NEW_IMAGE" < "$UNIT" > "$UNIT.new"; and mv "$UNIT.new" "$UNIT"
systemctl --user daemon-reload
systemctl --user start meeting-recorder.service
systemctl --user status meeting-recorder.service
```

For a data-preserving uninstall, stop the service, remove only the Quadlet, and
reload. This leaves recordings, config, state, cache, the Podman Speakr secret,
and Calendar Secret Service credentials in place.

```fish
systemctl --user stop meeting-recorder.service
rm -f "$HOME/.config/containers/systemd/meeting-recorder.container"
systemctl --user daemon-reload
podman ps -a --filter name=meeting-recorder
```

After checking that the stopped container is no longer needed, you may remove it
with `podman rm meeting-recorder`. Removing settings, recordings, Podman secrets,
or images is a separate destructive decision; verify the paths and any rollback
need first.
