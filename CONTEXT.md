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
version 1 stores the capture interval, original fallback filename, stable
occurrence selector, and current Meeting metadata. It is written atomically;
the Recording remains authoritative if sidecar or rename work fails.
_Avoid_: Metadata file, recording JSON

**Recording correction**:
An explicit cache-only operation under `meeting-recorder calendar correct` that
lists nearby fresh occurrences, selects one exact stable selector, or clears a
sidecar. It never performs a network refresh unless `--refresh` is requested and
never changes old recordings automatically after a later cache refresh.

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
start; without a valid sidecar, file mtime is the date fallback. The current path
and sidecar are reread after transfer before the authoritative metadata PATCH.
_Avoid_: Speakr plugin, Speakr connector

**Publication job**:
The durable record of one Recording's progress toward publication in Speakr.
Its durable progress fields remain public-only. It also contains one private
filesystem locator for the current Recording path, protected by the private
0700 directory and 0600 SQLite boundary; that locator is operational state,
not public publication data. It contains no API credentials and no copied or
frozen Meeting title, notes, or participants. Metadata is reread from the
Meeting sidecar at attempt time. Uncertain media transfers are not
automatically resent, metadata-pending jobs retry PATCH only, and published
reruns send nothing.
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

The private publication store uses schema v3. Its cleanup journal contains one
`cleanup_intents` record and exact `cleanup_intent_members` for each in-progress
operation; publication rows also carry the cleanup lease fence.
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
Debian package or Python packages with `pip`.

**Runtime host contract**:
The host-side services and runtime resources that the image does not provide:
the compositor, portal backend, PipeWire/Pulse server, buses, NetworkManager,
Secret Service keyring, browser, valid `TZ`, Wayland and Pulse sockets, session
bus, and writable XDG/Recording paths. Wayland capture receives the portal
connection; it does not require a raw PipeWire socket.

## Runtime image invariants

The image defaults `XDG_CONFIG_HOME` to `/config`, `XDG_STATE_HOME` to `/state`,
`XDG_CACHE_HOME` to `/cache`, and its source/work directory to
`/opt/meeting-recorder`. The `/usr/local/bin/meeting-recorder` launcher is the
entrypoint and its no-argument default is the `run` subcommand. It selects no
fixed user; ownership, persistence, and writable mounts are runtime concerns.

Daemon startup requires a valid `TZ`, Wayland socket, PulseAudio socket, and
session bus. Headless administrative commands remain available without
starting the daemon. Missing optional system bus access, Secret Service, Speakr
token, or Calendar OAuth configuration disables only the dependent optional
behavior; it does not redefine the base capture or administrative contract.

The image contract supports read-only roots, private temporary filesystems, and
arbitrary host-compatible UIDs, but it does not itself enforce deployment
confinement. No-new-privileges, capability drops, no devices or privileged mode,
no host networking, exact sockets and mounts, secret handling, SELinux choices,
and lifecycle are deployment invariants owned by #28 Quadlet. Registry and
versioned publishing are owned by #27, while real Bluefin validation is owned by
#29; this context does not claim production readiness or live capture validation.
