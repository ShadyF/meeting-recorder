# Meeting Recorder

The context covers consented meeting capture, enrichment, and optional publication while preserving standalone recording.

## Language

**Recording**:
A finalized local media file produced by a completed capture.
_Avoid_: Capture file, output file

**Completed Recording**:
An immutable description of a successfully finalized Recording and its optional
Meeting metadata snapshot. Enrichment returns a replacement value; it never
mutates the finalized result or media after dispatch.
_Avoid_: Recording result, completion event

**Meeting metadata**:
Calendar-derived identity for a recording, including its title, participants, scheduled time, description, and location.
_Avoid_: Calendar data, recording details

**Calendar match**:
The deterministic association between one Recording interval and one Google Calendar event instance.
_Avoid_: Event guess, calendar lookup

**Meeting sidecar**:
A versioned `<media filename>.meeting.json` file adjacent to a Recording. Schema
version 2 stores the capture interval, original fallback filename, stable
occurrence selector, current Meeting metadata, and an ordered selected Speakr tag
list (including an empty list). Schema v1 remains readable as no tags and is not
rewritten only to upgrade it. A sidecar is written atomically; the Recording
remains authoritative if sidecar or rename work fails.
_Avoid_: Metadata file, recording JSON

**Speakr tag**:
An existing Speakr tag represented by its stable positive integer ID and the
display name shown when it was selected. IDs are authoritative. A frozen ordered
selection is offered only for an explicitly accepted detected Recording while
automatic Publication is configured; it never delays or controls capture.
_Avoid_: Label, category

**Tag catalog**:
The caller's accessible Speakr tags returned in the personal-then-group API order.
It is fetched from `GET /api/v1/tags` for the exact `v0.10.5-alpha` integration
and may use only a transient-failure stale cache for the active normalized origin.
The cache contains no token and is not a publication-admission decision.

**Recording correction**:
An explicit cache-only operation under `meeting-recorder calendar correct` that
lists nearby fresh occurrences, selects one exact stable selector, or clears a
sidecar. It never performs a network refresh unless `--refresh` is requested and
never changes old recordings automatically after a later cache refresh.

**Google Calendar client secret**:
An optional OAuth credential for a Google Desktop client. Google's native-app
documentation describes this secret as optional, but some real Desktop client
configurations require it at the token endpoint. The `calendar client-secret
set|status|clear` commands manage it; `set` accepts the value only through a
hidden interactive TTY and stores it only in Secret Service, associated with the
configured public client ID. It is never stored in config, environment, argv,
credential JSON, or a Quadlet, and is never sent outside the token endpoint.
Only `set` requires an interactive TTY. For valid Secret Service storage,
`status` reports `absent`, `configured`, or `client-ID mismatch`; malformed or
unavailable storage fails safely without exposing stored contents. `clear` can
run without a TTY and removes the item even when the public client ID is absent
or malformed. `calendar disconnect` retains this secret.

**Capture mode**:
The selected media composition for a recording: audio-only or audio-video.
_Avoid_: Recording type, media mode

**Video source**:
The selected screen content for video capture: fullscreen, window, or area.
_Avoid_: Capture mode, screen mode

**Speakr publisher**:
The optional component that uploads a Recording and its selected public metadata
to Speakr, either for an explicit command or through the configured background
publication policy. A visible matched Meeting supplies current title/details and
scheduled start; a hidden matched Meeting supplies the current filename title and
non-private scheduled start without private notes/participants; an unmatched
Recording with a valid sidecar supplies the current filename title and capture
start; without a valid sidecar, file mtime is the date fallback. Before the first
media POST, it validates frozen tag IDs once against the fresh accessible catalog
within five seconds, then sends only the known IDs as contiguous multipart fields.
Transient tag validation preserves frozen IDs but records their result as unknown;
permanent or incompatible responses fail tags closed without blocking media
publication. The current path and sidecar are reread after transfer before the
authoritative metadata PATCH, which always omits tags. The publisher never changes
remote tags after upload.
_Avoid_: Speakr plugin, Speakr connector

**Publication job**:
The durable record of one Recording's progress toward publication in Speakr.
Its durable progress fields remain public-only. It also contains one private
filesystem locator for the current Recording path, protected by the private
0700 directory and 0600 SQLite boundary; that locator is operational state,
not public publication data. It contains no API credentials and no copied or
frozen Meeting title, notes, or participants. Metadata is reread from the
Meeting sidecar at attempt time. Its frozen tag snapshot and CLI-only tag outcome
are public: known `effective` and `missing` tag sets, an unknown upload outcome,
and a non-blocking local-sidecar rewrite warning. These warning fields persist
through `local_removed`. Uncertain media transfers are not automatically resent,
metadata-pending jobs retry PATCH only, and published reruns send nothing.
_Avoid_: Upload record, queue item

**Publication cleanup**:
An explicit CLI-only operation that previews or irreversibly removes a local
Recording and, when present, its exact adjacent Meeting sidecar after the
complete same-path publication group is confirmed: every job is `published`,
unleased, and records the same Recording SHA-256, and the Recording is older
than the requested cutoff. It does not run from the daemon, publication
automation, token or SSID admission, D-Bus, HTTP, or any other publication
path. Successful removal retains the group's publication history as
`local_removed`.

**Cleanup intent**:
The durable, restartable record of one explicit cleanup operation. It binds the
exact private path, Recording SHA-256, media identity, optional exact adjacent
Meeting sidecar identity, complete publication-job membership, and private
quarantine names to an ordered cleanup phase. It is not a Trash record and does
not provide local-file recovery.

## Publication cleanup invariants

The private publication store uses schema v4. Its cleanup journal contains one
`cleanup_intents` record and exact `cleanup_intent_members` for each in-progress
operation; publication rows also carry the cleanup lease fence. An exact v3 store
is permanently reset without an application backup on first v4 startup: queued
jobs, retries, remote IDs, and cleanup history are lost, but local Recordings
remain. This is not a migration and rollback to the prior binary is unsupported.
The journal is bounded to a complete same-path group, and the member set cannot
be replaced by a partial or newly added group while cleanup is in progress.

Only jobs in `published` may be claimed. Once claimed, every member must keep
the same private path and Recording SHA-256, have no publication lease, and hold
the cleanup lease and generation recorded for that intent. The intent stores the
media identity and, when present, the exact adjacent sidecar identity; a media
or sidecar identity, hash, path, or group change invalidates further cleanup.

The durable phases are ordered: `prepared`, `sidecar_quarantined`,
`media_quarantined`, `sidecar_unlinked`, and `media_unlinked`. Filesystem
namespace changes are checkpointed with directory durability before the journal
advances. An interrupted intent remains incomplete until a later explicit
`--delete` can validate and resume it. Completion changes every claimed job to
`local_removed`, clears its private path and leases, and retains the completed
publication audit chain, including the remote publication identity and ordered
publication timestamps.

**Runtime image**:
The Ubuntu 24.04 `linux/amd64` image built from the repository-root
`Containerfile`. It copies the source to `/opt/meeting-recorder`, executes the
application directly as `python3 -m meeting_recorder`, and supplies client-side
GTK/PyGObject, ffmpeg/GStreamer, portal, PulseAudio, D-Bus, and Secret Service
tooling. It is not the headless development container and does not install a
project application or Python packages with `pip`.

**Runtime host contract**:
The host-side services and runtime resources that the image does not provide:
the compositor, portal backend, PipeWire/Pulse server, buses, NetworkManager,
Secret Service keyring, browser, valid `TZ`, selected desktop transport, Pulse
socket, session bus, and writable XDG/Recording paths. The selected transport is
a Wayland socket for the Wayland profile, or the exact local X11 socket and
readable Xauthority file for the best-effort X11 profile. Wayland capture
receives the portal connection; it does not require a raw PipeWire socket.

**Container release**:
The GHCR publication of the Runtime image from a protected, manually managed
`vX.Y.Z[-prerelease]` tag. It has an immutable bare-version tag and
full-commit-SHA tag, plus a stable-only monotonic `latest` convenience tag. It
is not a deployment, installation, update, rollback, or graphical-capture
validation process.

## Runtime image invariants

The image defaults `XDG_CONFIG_HOME` to `/config`, `XDG_STATE_HOME` to `/state`,
`XDG_CACHE_HOME` to `/cache`, and its source/work directory to
`/opt/meeting-recorder`. The `/usr/local/bin/meeting-recorder` launcher is the
entrypoint and its no-argument default is the `run` subcommand. It selects no
fixed user; ownership, persistence, and writable mounts are runtime concerns.

Daemon startup requires a valid `TZ`, the selected desktop transport, a
PulseAudio socket, and a session bus. The selected transport is a Wayland socket
for the Wayland profile, or the exact local X11 socket and readable Xauthority
file for the best-effort X11 profile. Headless administrative commands remain
available without starting the daemon. Missing optional system bus access, Secret
Service, Speakr token, or Calendar OAuth configuration disables only the
dependent optional behavior; it does not redefine the base capture or
administrative contract.

The image contract supports read-only roots, private temporary filesystems, and
arbitrary host-compatible UIDs, but it does not itself enforce deployment
confinement. No-new-privileges, capability drops, no devices or privileged mode,
no host networking, exact sockets and mounts, secret handling, SELinux choices,
and lifecycle are deployment invariants owned by #28 Quadlet. Registry and
versioned publishing are owned by #27, while real Bluefin validation is owned by
#29; this context does not claim production readiness or live capture validation.

## Bluefin Quadlet invariants (#28)

The Bluefin deployment is a rootless Podman cgroup-v2 **user** Quadlet in the
active GNOME graphical session. It does not use lingering. Its generator-applied
`[Install] WantedBy=graphical-session.target` applies when the graphical target
is activated in a future GNOME session/login. A `daemon-reload` does not start a
new want for an already-active target, so the operator explicitly starts the
generated service for the current session; `PartOf=graphical-session.target`
stops it with that target. Real graphical lifecycle evidence remains with #29.
The `bluefin-v0.4.3` deployment unit pins the published v0.4.3 image digest
`sha256:ba80ec8bd7a70930eff15f12e8ed2cff0feb64d7ad6b9927e0817f24177829c6` from
release commit `fc9ee2841e9736430feb86c7f41dfe31a5fd7f1e`, with `Pull=never`.
The operator contract remains a digest-qualified image with
`Pull=never`; there are no
wrappers, installers, Compose deployment, auto-updates, host networking,
privileged mode, devices, broad home/runtime mounts, or raw PipeWire socket.

The deployment uses private, persistent config, state, and cache directories,
plus one absolute writable Recording directory. The image receives them at
`/config/meeting-recorder`, `/state/meeting-recorder`, and
`/cache/meeting-recorder`; the Recording directory has the **same absolute
path** on host and container. Configuration is therefore
`/config/meeting-recorder/config.json` in the container and
`$HOME/.config/meeting-recorder/config.json` on the host. That JSON fixes
`google_calendar_loopback_port` at `8765`, uses an absolute `output_dir`, may
contain only a bare public Google Desktop client ID, and contains neither a
Google refresh token, a Google client secret, nor a Speakr token. Some real
Desktop clients require a client secret despite Google's documentation calling
it optional; the `calendar client-secret set|status|clear` commands manage that
secret; only `set` requires an interactive TTY. `status` reports `absent`,
`configured`, or `client-ID mismatch` for valid Secret Service storage, while
malformed or unavailable storage fails safely without exposing stored contents.
`clear` works without a TTY and removes the item even when the public client ID
is absent or malformed. It is stored in Secret Service bound to the public
client ID and is used only at the token endpoint, never in config, environment,
argv, credential JSON, or the Quadlet. A Speakr bearer token, when
configured, is a rootless Podman secret file at
`/run/secrets/meeting-recorder-speakr-token`, owned by the user and mode `0400`.
It is never injected as an environment variable, placed in argv/history, or
written to configuration. The only published port is host `127.0.0.1:8765` to
the container callback port.

The Wayland variant mounts individual Wayland, Pulse, and session D-Bus sockets
at the active user's actual runtime paths: Wayland is `ro`, Pulse is `rw`, the
session D-Bus socket is `rw`, and the system D-Bus socket is `ro`.
Portal, notifications, and Secret Service use session D-Bus; NetworkManager
access uses the system bus. Managed-container Calendar `connect` requests the
host browser through the explicit XDG Desktop Portal OpenURI session-bus call;
it needs no GIO workaround or additional mount. The X11 alternative is the
supplied complete drop-in: it resets and re-adds the common volumes, then mounts
X11 `X0` and Xauthority `ro`, while retaining Pulse and session D-Bus `rw` and
system D-Bus `ro`. It remains a trusted, best-effort X11 path. `TZ`, runtime UID, Wayland
display, X11 display, and Xauthority paths are operator-specific values, not
portable defaults.

`ReadOnly=true` gives the container a read-only root filesystem; its bounded,
private `/tmp` is a writable tmpfs, not a read-only temporary mount.
`NoNewPrivileges` and all capability drops remain required.
`SecurityLabelDisable=true` is also required for this desktop socket integration:
it disables per-container SELinux process label separation while host SELinux
remains enforcing. This is a trusted desktop application rather than a strong
sandbox. Socket `ro` mounts do not limit protocol requests; session D-Bus can
reach portal, notifications, and potentially broader unlocked Secret Service
collections, while system D-Bus exposes more than NetworkManager and relies on
D-Bus policy and Polkit. `:Z` labels data mounts but does not restore process
separation.

`Restart=always` restarts a daemon that terminates after a successful settings
save once; an explicit stop remains stopped and graceful stop is allowed before
forced termination. Updates are manual: retain and record the old digest, pull
and edit to a verified new digest, and preserve the old local image for rollback.
The released v0.4.2 and v0.4.1 images ignore the client-secret item. If a build
with client-secret management is rolled back to either older image, a Desktop
client that requires a secret may fail to refresh. The Secret Service item
remains in place; rollback does not clear it. v0.4.3 consumes the item. Restore
v0.4.3 before using that client again, or use its explicit `calendar client-secret
clear` command when removal is intended. `calendar disconnect` retains the public
client configuration and client secret; only the explicit client-secret clear
operation removes that secret.
Quadlet-generated services are not enabled or disabled like ordinary units.
Uninstall stops the service, removes the Quadlet and optional X11 drop-in, then
reloads the user manager; container/image removal is optional. It preserves data,
Podman secrets, and Calendar Secret Service credentials by default; secret and
data removal is a separate destructive operator action. Real Bluefin logout/login,
portal consent, live capture, Recording output, Calendar portal/browser behavior,
update, and rollback evidence remains the responsibility of **#29** and is not
claimed by these invariants.

For v0.4.3 release commit `fc9ee2841e9736430feb86c7f41dfe31a5fd7f1e`, CI,
release, container publication, anonymous digest smoke, and real managed-container
Calendar connect/restart/refresh/disconnect/clear validation passed. This does
not claim broader live capture or logout/login validation.

## Container release invariants

Issue #27's least-privilege workflow publishes BuildKit maximum
provenance and an SBOM for each release digest, with GitHub Actions pinned by
full commit SHA. The pinned Ubuntu base and attestations establish source and
provenance reproducibility, not byte-for-byte rebuild reproducibility because
Ubuntu archive inputs are live.

The bare version and `sha-<full commit SHA>` tags cannot change digest. A
same-digest rerun is a no-op and a conflict fails. `latest` applies only to
stable releases, advances monotonically, and is never a deployment pin.
Digest-qualified consumption is the operator contract. First publication is
private until the owner makes it public and reruns the complete release; #27
remains incomplete until anonymous pull and hardened digest smoke succeed.
That smoke is release verification only, and no workflow or sample updates or
pulls an installed image automatically.
