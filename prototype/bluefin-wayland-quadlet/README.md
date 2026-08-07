# Bluefin Wayland Quadlet envelope — throwaway prototype

> **Disposable evidence envelope, not production packaging.** It proves one narrow
> Bluefin/Wayland path for the current app. It is not a supported replacement for the
> normal Meeting Recorder installation.

## One-command usage

From the repository root, in the target GNOME Wayland user's graphical session:

```bash
./prototype/bluefin-wayland-quadlet/prototype.sh up
```

This builds the local image, installs only the prototype user Quadlet, and starts its
generated service without `sudo`. `down` removes that unit and container while keeping
the dedicated prototype config and `Recordings` evidence.

## What the final envelope contains

The final Quadlet uses `UserNS=keep-id`, not an explicit `User=`. It ran as UID/GID
`1000:1000` in testing. It mounts exactly these session sockets:

- `/run/user/%U/wayland-0` read-only;
- `/run/user/%U/bus` read-write; and
- `/run/user/%U/pulse/native` read-write.

It also mounts only dedicated `%h` config and `Recordings` directories with private
`:Z` labels. The image supplies `HOME` and `XDG_CONFIG_HOME`; the Quadlet's four
explicit runtime variables are `GSETTINGS_BACKEND=memory`, `XDG_RUNTIME_DIR`,
`XDG_SESSION_TYPE=wayland`, and `PULSE_SERVER`.

`GSETTINGS_BACKEND=memory` prevents dconf runtime writes. `PULSE_SERVER` is required:
Pulse failed without it despite the mounted socket. `XDG_SESSION_TYPE=wayland` is
required for the app's portal path selection (`use_portal_capture` is false without
it and true with it when `WAYLAND_DISPLAY` is absent). With the exact mounts and
`XDG_RUNTIME_DIR`, GTK and D-Bus auto-discovered `wayland-0` and the session bus, so
explicit `WAYLAND_DISPLAY`, `GDK_BACKEND`, `DBUS_SESSION_BUS_ADDRESS`,
`XDG_CURRENT_DESKTOP`, and `XDG_SESSION_DESKTOP` were removed.

## Observed Recording results

The final envelope passed CLI/config read-write, GTK settings UI, desktop notification
actions, `pactl` detection, microphone/system-audio Recording, portal ScreenCast Recording,
ownership/persistence, lifecycle/restart, and boundary minimization. The user confirmed
the GTK window rendered and closed, clicked **Ignore** (the mounted config marker was
`ignore`), and confirmed both microphone and system audio were audible.

Portal ScreenCast Recording showed the chooser once, then reused its saved restore token
without a chooser. It created valid H.264 2048x1152 30fps MKVs, including a 22.566s
Recording and a later 269.433s Recording, with no `pipewire-0` mount. A repeated audio-only
Recording produced a valid 14.226s AAC stereo 48kHz MKV (308,160 bytes) owned by
`1000:1000`.

Audio-only degradation has one important limit. With Wayland and Pulse but no session
D-Bus, audio-only Recording continued and produced a host-owned file; notifications
fell back and AppIndicator warned. With Pulse/data but no Wayland or D-Bus, the app
exited 133 before recording because its tray/control GTK path created a style context
without a display. Therefore Wayland remains required even when `record_screen=false`.

## SELinux boundary conclusion

With default label separation, all three exact live sockets failed: Pulse was refused
and could not be `stat`ed, Gio was denied session D-Bus access, and Wayland was denied.
Audit records showed `container_t -> user_tmp_t` AVCs for Pulse/Wayland and
`container_t -> session_dbusd_tmp_t` for D-Bus; `sesearch` found no built-in allow.

`SecurityLabelDisable=true` passed for the exact sockets while the host remained SELinux
Enforcing. It disables SELinux label separation **only for this container**; it does not
enable privileged mode, add devices, widen mounts, or change host enforcement. This is a
material production limitation and requires a later production-contract review.

The live sockets intentionally have no `:z`/`:Z`: they are host session sockets, not
dedicated regular data directories. Relabeling them would alter the live session
boundary. This throwaway proof adds neither a custom SELinux module nor a proxy, because
either would be a separate production contract and would no longer prove the minimum
direct-socket envelope.

## Manual prerequisites

Run these non-mutating checks from the graphical-session user before `up`:

```bash
runtime_dir="/run/user/$(id -u)"
test -S "${runtime_dir}/wayland-0"
test -S "${runtime_dir}/bus"
test -S "${runtime_dir}/pulse/native"
systemctl --user is-active graphical-session.target xdg-desktop-portal.service pipewire.service pipewire-pulse.service wireplumber.service
getenforce
pactl info
```

The generated service is active under `graphical-session.target`; restart after a
SIGKILL was observed. A real graphical logout was not run because it would have ended
the HITL session, so `PartOf=` is statically verified but logout stop remains unobserved.

## Safety boundaries and limitations

- No `pipewire-0`, full `/run/user`, `/dev/video`, host network, devices, privileged
  mode, custom policy, or socket relabel was added. Successful GTK, notifications,
  audio Recording, and ScreenCast Recording show they are unnecessary here. ScreenCast
  uses a portal-granted PipeWire FD, not a raw video device.
- The prototype never touches normal app configuration or existing `Recordings`.
  Restart, `down`/`up`, and replacement preserved the dedicated config and four prior
  recordings.
- `meeting-recorder status` inside the container misleadingly reports its native
  service inactive because it cannot see the host user systemd manager.
- The host-side test timeout did not stop `podman exec`; an in-container timeout
  finalized Recording but could leave a defunct child below Python PID 1. This is a
  test/lifecycle limitation, not a production pass.

## Verdict

**Answered for this host:** a rootless graphical-session Quadlet can run the current
app on Bluefin Wayland with three exact sockets, two dedicated data mounts, `keep-id`,
the four explicit runtime variables above, and per-container label disable. It is viable
only as a throwaway evidence envelope, not production packaging, because label
separation is disabled and current audio-only mode still hard-depends on Wayland/GTK.
