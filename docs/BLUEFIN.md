# Bluefin GNOME / rootless Podman operator guide

This is the canonical direct-command guide for Meeting Recorder on a Bluefin
GNOME desktop. It runs the reviewed image as a rootless Podman **user** Quadlet
in the graphical desktop session. It is for consented Recording capture, not a
headless service or a production-validation claim.

This v0.4.3 release uses its published image digest and the Quadlet sets
`Pull=never`:

```text
ghcr.io/shadyf/meeting-recorder@sha256:ba80ec8bd7a70930eff15f12e8ed2cff0feb64d7ad6b9927e0817f24177829c6
```

The operator contract is digest-qualified. `Pull=never` keeps the user manager
from pulling or replacing this exact local image during a graphical session.

There is no installer or wrapper, Compose deployment, automatic pull or update,
host networking, privileged container, device access, extra capability, broad
home/runtime-directory mount, or raw PipeWire socket mount. Do not add any of
those to make a local problem appear to work.

## Obtain the matching deployment checkout

Pulling an image supplies no Quadlet or X11 drop-in files. Before copying either
file, obtain the deployment checkout or archive that contains the reviewed
deployment files for this digest. The commands below create a detached checkout;
an archive is equivalent only when it is from that same deployment tag and
contains the same reviewed files.

```bash
IMAGE='ghcr.io/shadyf/meeting-recorder@sha256:ba80ec8bd7a70930eff15f12e8ed2cff0feb64d7ad6b9927e0817f24177829c6'
DEPLOYMENT_TAG='bluefin-v0.4.3'
SOURCE_DIR="$HOME/src/meeting-recorder-$DEPLOYMENT_TAG"

git clone https://github.com/ShadyF/meeting-recorder.git "$SOURCE_DIR"
git -C "$SOURCE_DIR" checkout --detach "$DEPLOYMENT_TAG"
test "$(git -C "$SOURCE_DIR" for-each-ref --format='%(objecttype)' "refs/tags/$DEPLOYMENT_TAG")" = tag
test "$(git -C "$SOURCE_DIR" describe --exact-match --tags HEAD)" = "$DEPLOYMENT_TAG"
grep -Fx "Image=$IMAGE" "$SOURCE_DIR/deploy/bluefin/meeting-recorder.container"
test -f "$SOURCE_DIR/deploy/bluefin/meeting-recorder.container.d/50-x11.conf"
```

The deployment tag checks, `grep`, and file check confirm that the files copied
below are the reviewed deployment files. Do not copy deployment files from an
unrelated deployment checkout, branch, or later deployment tag. This remains a
direct copy procedure, not an installer.

## Security boundary and trust decision

> **`SecurityLabelDisable=true` disables per-container SELinux process-label
> separation. Host SELinux remains enforcing, but this is a trusted desktop
> application—not a strong sandbox. Do not use this configuration for an
> untrusted image or untrusted code.**

The desktop resources needed to capture a Recording are sensitive:

- A `ro` socket bind mount does **not** make the protocol read-only. A connected
  client can make every request that the Wayland, Pulse, or D-Bus service permits.
- The `rw` session D-Bus socket provides portals, notifications, and Secret
  Service access. An unlocked Secret Service collection can expose more than the
  Calendar credential.
- The `ro` system D-Bus socket is used for NetworkManager's optional Speakr Wi-Fi
  SSID admission check. It exposes more than NetworkManager; D-Bus policy and
  Polkit, not this mount, authorize individual calls.
- `:Z` relabels the mounted config, state, cache, and Recording paths for the
  container. It does not restore process-label separation.

The reviewed unit therefore exposes only individual sockets: Wayland `ro`,
Pulse `rw`, session D-Bus `rw`, and system D-Bus `ro`. The X11 profile adds only
X11 `X0` and Xauthority, both `ro`; it keeps Pulse and session D-Bus `rw` and
system D-Bus `ro`.

## Preconditions and host checks

Run every command as the desktop user in the local graphical GNOME login that
will own the service. Do not use `sudo`, a root shell, or SSH. The base profile
is for Wayland; use the complete supplied X11 drop-in only in an X11 session.

```bash
. /etc/os-release; printf '%s\n' "$PRETTY_NAME"
printf 'desktop=%s session=%s runtime=%s wayland=%s\n' \
  "$XDG_CURRENT_DESKTOP" "$XDG_SESSION_TYPE" "$XDG_RUNTIME_DIR" "$WAYLAND_DISPLAY"
loginctl show-session "$XDG_SESSION_ID" -p Type -p Class -p State -p Remote

test "$XDG_SESSION_TYPE" = wayland
test -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY"
test -S "$XDG_RUNTIME_DIR/pulse/native"
test -S "$XDG_RUNTIME_DIR/bus"
test -S /run/dbus/system_bus_socket

systemctl --user status xdg-desktop-portal.service xdg-desktop-portal-gnome.service
systemctl --user status pipewire.service pipewire-pulse.service wireplumber.service
busctl --user status org.freedesktop.portal.Desktop
busctl --user status org.freedesktop.secrets
pactl info
systemctl status NetworkManager.service
getenforce
podman info --format 'rootless={{.Host.Security.Rootless}} cgroup={{.Host.CgroupsVersion}}'
loginctl show-user "$USER" -p Linger
```

The intended facts are a local, active graphical GNOME session (`Remote=no`),
`rootless=true`, cgroup v2, and SELinux `Enforcing`. `pipewire-pulse` supplies
the Pulse socket; the raw PipeWire socket is deliberately absent. Portal consent
is supplied by the host portal and its GNOME backend. Calendar administration
requires an available, unlocked Secret Service; Speakr SSID admission requires
NetworkManager. Neither optional integration is required to make a Recording.

`Linger=no` is intentional. Do not enable lingering. The generated service's
`[Install] WantedBy=graphical-session.target` is applied by the Quadlet generator.
`systemctl --user daemon-reload` generates and reloads the unit but does **not**
start a newly added want for an already-active target; the explicit
`systemctl --user start` below handles the current session. `WantedBy` applies
when the graphical target is activated in a future session/login, as GNOME
supports. `PartOf=graphical-session.target` stops the service with that target.
The actual logout/login behavior remains unverified until **#29**; do not
interrupt an active Recording by logging out—stop it and wait for finalization
instead.

If a portal, Pulse, or Secret Service prerequisite is unavailable, repair the
desktop session. Do not compensate by mounting `/run/user/$UID`, all of `$HOME`,
or a host service directory.

## Create private host locations and configuration

The host configuration file is
`$HOME/.config/meeting-recorder/config.json`. The container sees that exact file
as `/config/meeting-recorder/config.json`. Create private host directories and a
single absolute Recording directory:

```bash
MR_CONFIG="$HOME/.config/meeting-recorder"
MR_STATE="$HOME/.local/state/meeting-recorder"
MR_CACHE="$HOME/.cache/meeting-recorder"
MR_RECORDINGS="$HOME/Videos/MeetingRecorder"

install -d -m 700 "$MR_CONFIG" "$MR_STATE" "$MR_CACHE" "$MR_RECORDINGS"
${EDITOR:-vi} "$MR_CONFIG/config.json"
```

Use a minimal override such as the following. Replace the sample `output_dir`
with the literal absolute path of `MR_RECORDINGS`; do not use `~` there.

```json
{
  "output_dir": "/home/alex/Videos/MeetingRecorder",
  "google_calendar_loopback_port": 8765,
  "google_calendar_client_id": "",
  "speakr_url": "",
  "speakr_publication_mode": "disabled",
  "speakr_allowed_ssids": []
}
```

The listener port, container port, and registered Google OAuth loopback redirect
are fixed at **8765** for this guide. Store only a bare public Google Desktop
client ID in `google_calendar_client_id`. Some real Google Desktop client
configurations also require a client secret. Never put that secret, credential
JSON, authorization code, refresh token, or Speakr bearer token in this JSON.
Manage a required client secret with the interactive `calendar client-secret`
commands below; it belongs only in the host Secret Service and is associated
with this public client ID. It is not a Podman secret or a Quadlet value.

## Pull the reviewed image and choose Speakr handling

Pull and inspect the published image digest before the service is
created. `Pull=never` means the user manager will not pull or replace it during
a graphical session.

```bash
podman pull "$IMAGE"
podman image inspect "$IMAGE"
```

The supplied base Quadlet contains a `Secret=` directive. Podman must resolve
that named secret when it creates the container; a missing secret is **not**
optional at container creation, even when Speakr publication is disabled.
Choose one of these paths before the first start:

1. **Use Speakr:** create the actual rootless-user secret from hidden standard
   input. The token is never put in `argv`, shell history, or configuration.

   ```bash
   read -r -s -p 'Speakr token: ' SPEAKR_TOKEN; printf '\n'
   printf %s "$SPEAKR_TOKEN" | podman secret create meeting-recorder-speakr-token -
   unset SPEAKR_TOKEN
   ```

2. **Do not use Speakr:** before starting, remove the single `Secret=` line from
   the copied Quadlet in the next section. Do not create a placeholder plaintext
   credential. With Speakr disabled and no direct Speakr command, the application
   does not need the secret file.

For the first path, Podman mounts the secret only as
`/run/secrets/meeting-recorder-speakr-token` with owner `uid=%U`, group `gid=%G`,
and mode `0400`. It is a file, never an environment variable. To rotate it,
stop the service, remove and recreate the secret from hidden standard input,
reload, and start the service. Rootless Podman secrets are scoped to this user.

## Install the Wayland Quadlet manually

Copy the reviewed source into the user Quadlet directory, then edit the **copied**
file. The Quadlet generator turns it into `meeting-recorder.service`; generated
Quadlet services are not enabled or disabled like ordinary unit files.

```bash
QUADLET_DIR="$HOME/.config/containers/systemd"
UNIT="$QUADLET_DIR/meeting-recorder.container"

install -d -m 700 "$QUADLET_DIR" "$QUADLET_DIR/meeting-recorder.container.d"
cp -- "$SOURCE_DIR/deploy/bluefin/meeting-recorder.container" "$UNIT"
${EDITOR:-vi} "$UNIT"
```

In the copied unit, make these operator-specific edits before reloading:

- Replace `Environment=TZ=Etc/UTC` with the desktop user's valid IANA timezone,
  for example `Environment=TZ=Europe/London`.
- If the session uses a display other than `wayland-0`, change **both** the
  Wayland `Volume=` source/target and `Environment=WAYLAND_DISPLAY=` to that
  actual socket path. `%t` expands to the user runtime directory and `%U` to the
  numeric UID.
- Change the Recording `Volume=` when needed. Its host source and container
  target must be the same absolute Recording path after specifier expansion; its
  `output_dir` must be that same absolute path in `config.json`.
- If you selected the no-Speakr path above, remove only the `Secret=` line. Do
  not add a token environment variable.

The persistent mounts are exactly
`%h/.config/meeting-recorder:/config/meeting-recorder:Z`,
`%h/.local/state/meeting-recorder:/state/meeting-recorder:Z`, and
`%h/.cache/meeting-recorder:/cache/meeting-recorder:Z`. The Recording mount uses
the same absolute path on both sides. `ReadOnly=true` makes the root filesystem
read-only; `/tmp` is instead a bounded, private **writable** tmpfs
(`rw,nosuid,nodev,noexec,size=64m`). Keep `NoNewPrivileges=true` and
`DropCapability=all`.

Reload the generator output, then explicitly start and inspect the service:

```bash
systemctl --user daemon-reload
systemctl --user start meeting-recorder.service
systemctl --user status meeting-recorder.service
podman ps --filter name=meeting-recorder
journalctl --user -u meeting-recorder.service -f
```

`PublishPort=127.0.0.1:8765:8765/tcp` exposes the Calendar callback only on
host loopback. Keep it loopback-only and retain `Pull=never`; no automatic pull,
update, or rollback occurs.

### X11 alternative: install the supplied complete drop-in

Use this alternative only when the active session reports `x11`. Verify the
display resources first:

```bash
printf 'display=%s xauthority=%s\n' "$DISPLAY" "${XAUTHORITY:-$HOME/.Xauthority}"
test "$XDG_SESSION_TYPE" = x11
X11_NUMBER="${DISPLAY#:}"; X11_NUMBER="${X11_NUMBER%%.*}"
test -S "/tmp/.X11-unix/X$X11_NUMBER"
test -r "${XAUTHORITY:-$HOME/.Xauthority}"
```

Copy the supplied **full** drop-in and edit the copied file. Do not replace it
with a shortened snippet.

```bash
X11_DROP_IN="$HOME/.config/containers/systemd/meeting-recorder.container.d/50-x11.conf"
cp -- "$SOURCE_DIR/deploy/bluefin/meeting-recorder.container.d/50-x11.conf" "$X11_DROP_IN"
${EDITOR:-vi} "$X11_DROP_IN"
```

`Volume=` with an empty value at the start of this file resets the inherited
`Volume=` list. The supplied drop-in then re-adds **all** persistent data mounts,
the same-path Recording mount, Pulse `rw`, session D-Bus `rw`, system D-Bus
`ro`, X11 `X0` `ro`, and Xauthority `ro`. It does not add X11 access to the
Wayland list. Edit its copied `DISPLAY`, `XAUTHORITY`, X socket, and Xauthority
mount together for the actual session; `X0` is valid only for `DISPLAY=:0`.

This is the normal X11 trust model: an Xauthority cookie permits a connected
client to control the display, and read-only file/socket mounts do not make that
interaction safe or one-way. Do not use it for an untrusted container.

Apply the profile and restart the generated service:

```bash
systemctl --user daemon-reload
systemctl --user restart meeting-recorder.service
systemctl --user status meeting-recorder.service
```

To return to Wayland, remove only the copied `50-x11.conf`, restore any current
Wayland-specific edits in the base copied Quadlet, reload the user manager, and
restart the service.

## Service lifecycle and direct administrative commands

Use systemd on the host for service state. Do **not** run
`meeting-recorder status` inside the container: it checks the unavailable native
host service and is misleading for this deployment.

```bash
systemctl --user status meeting-recorder.service
systemctl --user stop meeting-recorder.service
systemctl --user start meeting-recorder.service
journalctl --user -u meeting-recorder.service --since '10 minutes ago'
```

An explicit stop remains stopped despite `Restart=always`. Allow the configured
20-second container stop timeout and 30-second service timeout for a Recording
to finalize; do not use `podman kill` or logout as a stop method. A successful
settings save deliberately ends the daemon, and `Restart=always` starts it once
after its 10-second restart delay. Do not add an extra manual restart after that
successful save; investigate the journal if it loops instead.

Run application operations with `podman exec` while the container is running.
Because the Recording directory is mounted at the same absolute path, use that
same path in direct commands. The parser supports these forms:

```bash
# Open settings; a successful save triggers the managed restart described above.
podman exec meeting-recorder meeting-recorder settings

# Create or print the container-visible config: /config/meeting-recorder/config.json.
podman exec meeting-recorder meeting-recorder config

# Calendar connect uses the session-bus OpenURI portal to request the host browser.
# Only client-secret set needs an attached TTY for hidden input.
# Calendar administration requires the host session bus and unlocked Secret Service.
podman exec meeting-recorder meeting-recorder calendar connect
podman exec meeting-recorder meeting-recorder calendar status
podman exec meeting-recorder meeting-recorder calendar disconnect
podman exec -it meeting-recorder meeting-recorder calendar client-secret set
podman exec meeting-recorder meeting-recorder calendar client-secret status
podman exec meeting-recorder meeting-recorder calendar client-secret clear
podman exec meeting-recorder meeting-recorder calendar list
podman exec meeting-recorder meeting-recorder calendar select --id 'calendar-id'
podman exec meeting-recorder meeting-recorder calendar refresh
podman exec meeting-recorder meeting-recorder calendar correct "$MR_RECORDINGS/example.mkv"

# Speakr operations require the configured secret file and remain SSID-gated.
podman exec meeting-recorder meeting-recorder speakr upload "$MR_RECORDINGS/example.mkv"
podman exec meeting-recorder meeting-recorder speakr upload --all
podman exec meeting-recorder meeting-recorder speakr upload --status --all
podman exec meeting-recorder meeting-recorder speakr upload --retry JOB
podman exec meeting-recorder meeting-recorder speakr upload --relink JOB "$MR_RECORDINGS/relinked.mkv"
podman exec meeting-recorder meeting-recorder speakr upload --forget JOB

# Preview first. --delete is irreversible for eligible published Recordings.
podman exec meeting-recorder meeting-recorder cleanup --older-than 30
podman exec meeting-recorder meeting-recorder cleanup --older-than 30 --delete
```

`calendar client-secret set` prompts for the secret without displaying it. It
is the only client-secret operation that needs an attached interactive TTY as
shown above; do not pass the secret in an argument, pipe it through standard
input, put it in a shell variable, or add it to the Quadlet. For valid Secret
Service storage, `client-secret status` reports `absent`, `configured`, or
`client-ID mismatch` without revealing the secret. Malformed or unavailable
Secret Service storage fails safely without exposing stored contents.
`client-secret clear` explicitly removes the secret, can run without a TTY, and
remains usable even when the public client ID is absent or malformed. The secret
is used only at Google's token endpoint; it is not sent to the browser
authorization URL or Calendar API requests.

`calendar disconnect` revokes the refresh token on a best-effort basis and
removes the local refresh token and Calendar-only cache. It retains the public
client ID, loopback configuration, and client secret. Use `calendar client-secret
clear` when the secret itself must be removed. None of these commands change
the reviewed socket mounts, privileges, capability policy, SELinux setting, or
trusted-desktop security boundary; the client secret uses the existing session-
bus Secret Service path.

`--force` is accepted with Speakr `upload PATH`, `upload --all`, and
`upload --retry JOB`; it bypasses only the SSID gate. Cleanup preview does not
change data. `--delete` can remove eligible published Recordings and their exact
adjacent Meeting sidecars. `podman exec ... calendar connect` requests the host
browser through the explicit XDG Desktop Portal OpenURI call on session D-Bus.
It needs no `GIO_USE_PORTALS` workaround or additional mount. Live portal/browser
proof remains part of **#29**.

### Fixed Calendar OAuth callback: `127.0.0.1:8765`

Before `calendar connect`, verify that the host port is free and the running
container has the expected loopback mapping:

```bash
ss -ltnp 'sport = :8765'
podman port meeting-recorder
systemctl --user status meeting-recorder.service
```

If start fails because port 8765 is occupied, stop or reconfigure the conflicting
local listener, then start the service again. Do not silently choose another
port: `config.json`, `PublishPort`, and the Google OAuth redirect registration
must stay on `8765` (`127.0.0.1` on the host). If browser consent cannot complete,
verify that the service is running and that the browser is on this host; do not
expose the callback beyond loopback.

## Manual update and rollback

There are no automatic image updates. Stop cleanly, retain the old local image,
pull and inspect a verified new digest, edit only `Image=` in the copied Quadlet,
reload, and start. Use this procedure to update from the published v0.4.2 digest
to the published v0.4.3 digest:

> **v0.4.2/v0.4.1 rollback caveat:** Those older images ignore the
> Secret Service client-secret item. If a build with `calendar client-secret`
> support is rolled back to v0.4.2 or v0.4.1, a Google Desktop client that
> requires a secret may fail to refresh. The Secret Service item remains in
> place; rollback does not clear it. v0.4.3 consumes the item. Restore v0.4.3
> before using that client again, or clear the secret with a supporting build
> before intentional credential removal. Do not treat image rollback as secret
> deletion.

```bash
UNIT="$HOME/.config/containers/systemd/meeting-recorder.container"
OLD_IMAGE='ghcr.io/shadyf/meeting-recorder@sha256:766eb08d1270d5ff6276e41ae31fe85a3ad2593fe5145273da5004ac3edc562b'
NEW_IMAGE='ghcr.io/shadyf/meeting-recorder@sha256:ba80ec8bd7a70930eff15f12e8ed2cff0feb64d7ad6b9927e0817f24177829c6'

systemctl --user stop meeting-recorder.service
podman image inspect "$OLD_IMAGE"
podman pull "$NEW_IMAGE"
podman image inspect "$NEW_IMAGE"
cp -- "$UNIT" "$UNIT.previous"
${EDITOR:-vi} "$UNIT"
systemctl --user daemon-reload
systemctl --user start meeting-recorder.service
systemctl --user status meeting-recorder.service
```

Keep `Pull=never`, all exact mount modes, port policy, and confinement settings.
If the new image fails, restore the saved copied Quadlet while the old image is
still local:

```bash
systemctl --user stop meeting-recorder.service
cp -- "$UNIT.previous" "$UNIT"
systemctl --user daemon-reload
systemctl --user start meeting-recorder.service
systemctl --user status meeting-recorder.service
journalctl --user -u meeting-recorder.service --since '10 minutes ago'
```

Record the verified new digest before later image cleanup. Do not prune the old
image until it is no longer a rollback option.

## Data-preserving uninstall and optional destructive removal

Generated Quadlet services are **not** disabled with `systemctl disable`. Normal
uninstall stops the service, removes the user Quadlet and its optional X11
drop-in, and reloads the user manager. It preserves config, state, cache,
Recordings, Podman secrets, and Calendar Secret Service credentials.

```bash
systemctl --user stop meeting-recorder.service
rm -f "$HOME/.config/containers/systemd/meeting-recorder.container"
rm -f "$HOME/.config/containers/systemd/meeting-recorder.container.d/50-x11.conf"
rmdir --ignore-fail-on-non-empty "$HOME/.config/containers/systemd/meeting-recorder.container.d"
systemctl --user daemon-reload
podman ps -a --filter name=meeting-recorder
```

After confirming the displayed container is no longer needed, optional cleanup
may remove it and, only after rollback is no longer needed, its image:

```bash
podman rm meeting-recorder
podman image rm "$IMAGE"
```

The following is a separate destructive decision. It removes the Podman secret
when present and all private application data, including every Recording and
adjacent Meeting sidecar. It does not remove a Google Calendar client secret
from Secret Service; use `calendar client-secret clear` with a supporting image
when that secret must be removed. `MR_RECORDINGS` must be copied from the **verified**
absolute `output_dir` in `config.json` after confirming it is the same absolute
container target in the copied Quadlet. Do not substitute the original example
path after customizing the installation. Print and review that explicit path;
there is no application recovery path.

```bash
MR_RECORDINGS='/absolute/path/verified-in-config-and-quadlet'
case "$MR_RECORDINGS" in
  /) printf '%s\n' 'Refusing to remove /' >&2; exit 1 ;;
  /*) ;;
  *) printf '%s\n' 'MR_RECORDINGS must be absolute' >&2; exit 1 ;;
esac
printf 'DESTRUCTIVE: this will remove Recordings at: %s\n' "$MR_RECORDINGS"
if ! read -r -p 'Type DELETE-RECORDINGS to continue: ' CONFIRM; then
  printf '%s\n' 'Destructive removal cancelled: no confirmation was read.' >&2
  exit 1
fi
if [ "$CONFIRM" != DELETE-RECORDINGS ]; then
  printf '%s\n' 'Destructive removal cancelled: confirmation did not match.' >&2
  exit 1
fi

podman secret rm meeting-recorder-speakr-token
rm -rf -- "$HOME/.config/meeting-recorder" \
  "$HOME/.local/state/meeting-recorder" \
  "$HOME/.cache/meeting-recorder" \
  "$MR_RECORDINGS"
```

## Validation boundary

This document specifies the intended operator configuration and commands. For
v0.4.3, CI, release, container publication, anonymous digest smoke, and real
managed-container Calendar connect/restart/refresh/disconnect/clear validation
passed. Those results do not claim broader live capture, logout/login, or other
graphical-host validation; that evidence remains the responsibility of
**issue #29**, the real-host validation owner.
