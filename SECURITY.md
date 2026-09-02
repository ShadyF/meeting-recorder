# Security Policy

## Supported versions

This project is pre-1.0; only the latest release receives fixes.

| Version | Supported |
|---------|-----------|
| 0.1.x   | ✅        |
| < 0.1   | ❌        |

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Use GitHub's private reporting:
[**Report a vulnerability**](https://github.com/ssKazal/meeting-recorder/security/advisories/new)

Please include what you did, what happened, and the impact. You can expect an
acknowledgement within a few days.

## Scope — why this matters here

This application can record your screen, microphone and system audio, so
please pay particular attention to:

- Anything that causes a recording to start **without the user's consent**
  (the permission prompt being bypassed or spoofed).
- Anything that discloses recordings, or writes them somewhere world-readable.
- Command injection via configuration values that reach `ffmpeg` (for example
  `capture_region`, `noise_model_path` or `output_dir`).

## Design notes relevant to security

- The daemon runs as a **systemd user service** with your own privileges — never
  as root. It deliberately is not a system service.
- Recordings are written to `~/Videos/MeetingRecorder/` (configurable) and owned
  by you.
- Speakr publication follows `speakr_publication_mode`: `disabled` creates no
  automatic jobs or daemon attempts, `manual` retries existing explicit jobs,
  and `automatic` creates automatic publication jobs only for newly completed
  Recordings from the daemon or `meeting-recorder record`. Explicit
  `meeting-recorder speakr upload ...` commands remain available in every mode.
  The daemon never scans historical directories to create publication jobs.
- An empty valid `speakr_allowed_ssids` list disables the SSID safety gate.
  When the list is nonempty, normal network attempts (`upload PATH`, `upload
  --all`, and `upload --retry JOB`) require NetworkManager to report an active
  Wi-Fi SSID that exactly matches it. A confirmed active non-Wi-Fi connection
  bypasses this SSID-only gate. Entries are case-sensitive raw UTF-8 strings, at
  most 32 UTF-8 bytes, with no trimming or normalization. Unknown, unavailable,
  transitional, or nonmatching active Wi-Fi waits without contacting Speakr.
  Invalid SSID configuration fails closed. `--force`
  is accepted only for those network forms and bypasses only the SSID gate;
  HTTPS, authentication, file identity, lease, reconciliation, and other state
  and file-safety checks still apply. An SSID match is not authentication.
- Production `speakr_url` must use HTTPS. The bearer token is accepted only from
  `MEETING_RECORDER_SPEAKR_TOKEN`; it is never stored in configuration, CLI
  arguments, or SQLite.
- Speakr's local publication database stores public-only durable progress fields
  plus one protected private filesystem locator for the current Recording path
  as operational state. The locator is kept inside the protected 0700 state
  directory and 0600 SQLite boundary under `XDG_STATE_HOME` (or the user's
  local state directory); the database contains no credentials or copied
  private Meeting title, notes, or participants.
- Detection reads audio-stream metadata via `pactl`; it never reads audio content
  to decide whether to record.

### Explicit local publication cleanup

`meeting-recorder cleanup --older-than DAYS` is a preview. `DAYS` must be at
least `1`; only `--delete` performs irreversible local deletion. Cleanup is
CLI-only and is not started by the daemon, publication automation, a token or
SSID check, D-Bus, HTTP, or any other publication path.

Deletion is limited to an old Recording path whose complete same-path group has
only `published`, unleased jobs with one matching Recording SHA-256. Cleanup
hashes the media again and checks the exact adjacent
`<media filename>.meeting.json` sidecar when one exists. It fails closed for
unsafe or malformed entries, identity or hash mismatches, symlinks, hardlinks,
out-of-root paths, and changes detected during the operation, stopping with an
incomplete result rather than trusting those entries. It removes only the media
and that exact sidecar, not unrelated neighboring files.

The destructive path first records a durable intent, uses private quarantine
names, and fsyncs namespace changes before advancing its journal phase. A crash
can leave an intent or quarantine entry; only a later explicit `--delete` may
resume it, after the recorded group and file identities still validate. There
is no Trash or user recovery mechanism. A completed cleanup retains each
publication job as `local_removed` with its publication history, while removing
the local path from the job.

## Runtime image boundary

The runtime image is built from the repository-root `Containerfile` for Ubuntu
24.04 `linux/amd64`. It is designed to tolerate a read-only root filesystem,
private temporary filesystems, and an arbitrary host-compatible UID when the
runtime supplies the writable paths and required sockets. These are image
compatibility properties, not deployment guarantees.

The image does not by itself establish the security boundary for a deployment.
Issue #28 Quadlet owns the actual `no-new-privileges` setting, capability
drops, absence of devices and privileged mode, absence of host networking,
exact socket and mount exposure, secret injection, SELinux trade-offs, and
service lifecycle. This documentation intentionally does not provide that
deployment recipe. Issue #29 owns real Bluefin validation; this image contract
does not claim production readiness or live capture validation.

The compositor, portal backend, PipeWire/Pulse server, buses, NetworkManager,
Secret Service keyring, browser, and other desktop services remain host-side.
The image uses client tooling and does not assume a raw PipeWire socket. A
runtime that exposes more sockets, devices, mounts, or credentials than the
application needs is outside this contract.

Daemon preflight still requires a valid `TZ`, Wayland socket, PulseAudio
socket, and session bus; headless administrative commands do not require the
daemon to start. Missing the system bus, Secret Service, Speakr token, or
Calendar OAuth configuration disables only the dependent optional behavior.

## Container image publication

Issue #27 defines a least-privilege GHCR workflow for the intended
`ghcr.io/shadyf/meeting-recorder` image. It accepts protected, manually managed
release tags and pins every GitHub Action by full commit SHA. Releases attach
BuildKit maximum provenance and an SBOM to the image digest. The base Ubuntu
image is pinned, but live Ubuntu archive inputs mean these attestations support
source and provenance reproducibility, not a guarantee of byte-for-byte rebuilt
images.

Version and full-commit-SHA tags are immutable: same-digest reruns do nothing,
while a conflicting tag fails. Stable `latest` is monotonic and never rolls
back; it is a convenience reference, not a deployment pin. Consume a verified
release by digest. The detailed release, visibility, and consumption policy is
in [Container images](docs/CONTAINER-IMAGES.md).

The first GHCR image defaults to private. The owner must make it public and
rerun the complete workflow; #27 remains incomplete until an anonymous digest
pull and hardened smoke pass. That smoke verifies a release artifact only. It
does not deploy, install, update, or validate graphical capture, and no workflow
or sample automatically pulls or updates installations.
