# Evidence — Bluefin Wayland Quadlet envelope

> **Completed throwaway prototype record.** This is evidence for one Bluefin Wayland
> host, not production packaging approval.

## Baseline and build

| Fact | Observed result |
| --- | --- |
| Host/session | Bluefin 44; GNOME Wayland session 2 |
| Runtime | Podman 5.8.4 rootless; systemd 259; crun |
| Host state | SELinux Enforcing; exact `wayland-0`, `bus`, and `pulse/native` sockets present; requested services active |
| Tests | 38/38 passed |
| Image | Ubuntu 24.04; final size 710,467,357 bytes; initial build ID `74c84086...`, final rebuild ID `870cb945...` (IDs are rebuild-specific) |
| Config CLI | PASS; isolated write mode `0644`, UID/GID `1000:1000` |
| Generator | Initial `Remove=true` was invalid and removed; final `QUADLET_UNIT_DIRS` dry-run PASS |

## SELinux result

| Mode | Result | Evidence |
| --- | --- | --- |
| Default label separation | FAIL | Pulse, D-Bus, and Wayland denied; AVCs `container_t -> user_tmp_t` (Pulse/Wayland) and `container_t -> session_dbusd_tmp_t` (D-Bus); no built-in `sesearch` allow |
| `SecurityLabelDisable=true` | PASS | Exact sockets work while host SELinux remains Enforcing |

This per-container label-separation exception is a material production limitation.

## Required pass/fail matrix

| # | Experiment | Result | Evidence |
| --- | --- | --- | --- |
| 1 | CLI/config RW | PASS | Isolated config write was `0644`, `1000:1000` |
| 2 | GTK settings UI | PASS | User confirmed it rendered and closed; GTK initialized `wayland-0` |
| 3 | Desktop notifications/actions | PASS | User clicked Ignore; mounted config marker contained `ignore` |
| 4 | `pactl` detection | PASS | In-container ffmpeg Mic consumer appeared as non-monitor output `Lavf60.16.100`, binary `ffmpeg` |
| 5 | Mic/system-audio Recording | PASS | Repeated audio-only 14.226s AAC stereo 48kHz MKV, 308,160 bytes, `1000:1000`; user confirmed both audio sources |
| 6 | Portal ScreenCast Recording | PASS | User approved chooser; H.264 2048x1152 30fps MKV 22.566s; saved token later produced valid 269.433s Recording without chooser |
| 7 | Audio-only without portal/Wayland | PARTIAL / FAIL | No D-Bus with Wayland/Pulse: Recording PASS with notification fallback; no Wayland/D-Bus: exited 133 before capture |
| 8 | Ownership/restart persistence | PASS | Outputs `1000:1000`; failure restart preserved config and four recordings; `down`/`up` preserved hash/count |
| 9 | `graphical-session.target` Quadlet lifecycle/restart | PASS with limitation | Active under target; generated `After`, `PartOf`, `WantedBy`; SIGKILL restarted (`NRestarts=1`) and replaced container; logout stop unobserved |
| 10 | Candidate boundary removals | PASS | Results recorded below |

The GTK test emitted an at-spi accessibility-bus warning because that bus was not
mounted. `GSETTINGS_BACKEND=memory` avoided dconf runtime writes.

## Boundary-removal and minimization results

| Boundary or setting | Result | Conclusion |
| --- | --- | --- |
| Wayland socket removed | FAIL | GTK fails; required even for audio-only Recording |
| Session D-Bus socket removed | PARTIAL | Gio/notifications/portal fail; Wayland+Pulse audio-only Recording continues with notification fallback |
| Pulse socket removed | FAIL | `pactl` connection refused; required for detection/audio |
| Dedicated config mount removed | FAIL | Config creation `PermissionError`; required for config and token persistence |
| Dedicated `Recordings` mount removed | FAIL | Output write `PermissionError`; required for Recording persistence |
| `SecurityLabelDisable` removed | FAIL | All live sockets denied; required for this direct-socket prototype |
| `pipewire-0`, full runtime dir, `/dev/video`, host network, devices, privileged mode, custom policy, socket relabel | Never added; full feature set PASS | Unnecessary on this host; ScreenCast uses a portal-granted PipeWire FD, not a raw video device |
| Explicit `User=%U` removed | PASS | `UserNS=keep-id` alone ran `1000:1000`; Pulse and D-Bus tests passed |
| Explicit Wayland/GDK/D-Bus/desktop variables removed | PASS | With exact mounts and `XDG_RUNTIME_DIR`, GTK and D-Bus auto-discovered `wayland-0` and bus |
| `PULSE_SERVER` removed | FAIL | Pulse failed despite mounted socket |
| `XDG_SESSION_TYPE=wayland` removed | FAIL | `use_portal_capture` false without it and true with it when `WAYLAND_DISPLAY` is absent |

## Final exact requirement table

| Requirement | Final implementation | Result |
| --- | --- | --- |
| Image | Local Ubuntu 24.04 image | PASS; rebuild-specific IDs above |
| Unit lifecycle | `After=`/`PartOf=` graphical target; sole `[Install] WantedBy=` | PASS, except logout stop not observed |
| Identity | `UserNS=keep-id`; no explicit `User=` | PASS |
| Sockets | Exact Wayland (ro), D-Bus (rw), Pulse (rw) binds only | PASS with `SecurityLabelDisable=true` |
| Data | Dedicated `%h` config and `Recordings` binds with `:Z` only | PASS |
| Runtime environment | `GSETTINGS_BACKEND=memory`, `XDG_RUNTIME_DIR`, `XDG_SESSION_TYPE=wayland`, `PULSE_SERVER` | PASS; image supplies `HOME`/`XDG_CONFIG_HOME` |
| SELinux | Per-container `SecurityLabelDisable=true`; host Enforcing | PASS; material production limitation |
| Broad access | No PipeWire socket, runtime-dir bind, video device, host network, devices, privileged mode, policy, or socket relabel | PASS unnecessary |

## Rejected alternatives

| Alternative | Reason |
| --- | --- |
| Default SELinux label separation | Denies all required live sockets; no built-in allow found |
| `:z`/`:Z` on live sockets | Would relabel host session sockets; only dedicated regular data dirs use `:Z` |
| Custom SELinux module or proxy | Separate production contract; outside the direct-socket proof |
| `pipewire-0`, full runtime dir, `/dev/video`, host network, devices, privileged mode | Not needed by successful GTK, notifications, audio Recording, or ScreenCast Recording |
| Explicit `User=%U` and removed desktop variables | Proven unnecessary by minimization tests |
| `Remove=true` | Unsupported by the Podman 5.8.4 generator |

## Limitations

- Label separation is disabled for this container. That makes this unsuitable as
  production packaging without a new reviewed security contract.
- Current audio-only Recording still needs Wayland because the tray/control GTK path
  creates a display-dependent style context before capture.
- `meeting-recorder status` in the container reports its native service inactive because
  it cannot see host user systemd.
- Host-side timeout did not stop `podman exec`; an in-container timeout finalized but
  could leave a defunct child under Python PID 1. This is a test/lifecycle limitation.

## Verdict

**Answered:** on this Bluefin Wayland host, the current app runs in a rootless
graphical-session Quadlet using three exact sockets, two dedicated data mounts,
`keep-id`, four explicit runtime variables, and per-container label disable. It remains
a throwaway evidence envelope—not production packaging—because of the SELinux exception
and the app's Wayland/GTK dependency for audio-only Recording.
